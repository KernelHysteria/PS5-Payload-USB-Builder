#!/usr/bin/env python3
"""
PS5 Payload USB Builder  ·  v1.2
================================
A polished desktop GUI for building a payload USB folder with autoload.txt.

- Pulls release lists (official / prerelease / draft) from GitHub / Gitea
  payload repos defined in sources.json (next to the .py / .exe).
- Browse Source → Release → Asset in a tree with checkboxes.
- Expand .zip assets to pick individual files (usually the .elf).
- Build an ordered load sequence with per-payload delays.
- "Build USB Folder" downloads everything and writes autoload.txt:

      !1000
      some_payload.elf

Only uses the Python standard library (tkinter, urllib, zipfile, json).

sources.json lives next to the script (or the .exe). Edit it to add/remove
repos without touching the code. When frozen with PyInstaller, place
sources.json beside the exe, or bundle it with --add-data.
"""

import io
import json
import os
import queue
import shutil
import sys
import threading
import tempfile
import zipfile
import urllib.request
import urllib.error
from datetime import datetime

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ---------------------------------------------------------------------------
# Paths  (works for both .py and frozen .exe via PyInstaller etc.)
# ---------------------------------------------------------------------------
def _app_dir():
    """Directory that contains the script / exe (and ideally sources.json)."""
    if getattr(sys, "frozen", False):
        # PyInstaller / cx_Freeze / Nuitka one-file or one-dir
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _resource_path(name):
    """Locate a data file next to the app, or inside a PyInstaller bundle."""
    # 1. Next to the exe / script (user can edit sources.json easily)
    candidate = os.path.join(_app_dir(), name)
    if os.path.isfile(candidate):
        return candidate
    # 2. Inside the bundle (sys._MEIPASS for one-file PyInstaller)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = os.path.join(meipass, name)
        if os.path.isfile(candidate):
            return candidate
    return os.path.join(_app_dir(), name)  # fallback path (will raise later)


# ---------------------------------------------------------------------------
# Source definitions  (loaded from sources.json next to the app)
# ---------------------------------------------------------------------------
DEFAULT_SOURCES = [
    {"name": "BackPork", "type": "github", "owner": "BestPig", "repo": "BackPork"},
    {"name": "CheatRunner", "type": "github", "owner": "notmaj0r", "repo": "CheatRunner",
     "tags_fallback": True},
    {"name": "elfldr", "type": "github", "owner": "ps5-payload-dev", "repo": "elfldr"},
    {"name": "etaHEN", "type": "github", "owner": "etaHEN", "repo": "etaHEN"},
    {"name": "ftpsrv", "type": "github", "owner": "ps5-payload-dev", "repo": "ftpsrv"},
    {"name": "garlic-savemgr", "type": "gitea", "base": "https://git.etawen.dev",
     "owner": "earthonion", "repo": "garlic-savemgr"},
    {"name": "klogsrv", "type": "github", "owner": "ps5-payload-dev", "repo": "klogsrv"},
    {"name": "kstuff", "type": "github", "owner": "EchoStretch", "repo": "kstuff"},
    {"name": "kstuff-lite (drakmor)", "type": "github", "owner": "drakmor", "repo": "kstuff-lite"},
    {"name": "kstuff-lite (EchoStretch)", "type": "github", "owner": "EchoStretch", "repo": "kstuff-lite"},
    {"name": "kstuff-toggle", "type": "github", "owner": "EchoStretch", "repo": "kstuff-toggle"},
    {"name": "nanoDNS", "type": "github", "owner": "drakmor", "repo": "nanoDNS"},
    {"name": "pldmgr", "type": "github", "owner": "itsPLK", "repo": "ps5-payload-manager"},
    {"name": "ps5-app-dumper", "type": "github", "owner": "EchoStretch", "repo": "ps5-app-dumper"},
    {"name": "ps5-linux-loader", "type": "github", "owner": "ps5-linux", "repo": "ps5-linux-loader"},
    {"name": "ps5debug-NG", "type": "github", "owner": "OpenSourcereR-dev", "repo": "ps5debug-NG"},
    {"name": "PS5-Lapy-JB-Daemon", "type": "github", "owner": "itsPLK", "repo": "PS5-Lapy-JB-Daemon"},
    {"name": "PS-Game-Patch (patch XML)", "type": "github", "owner": "illusionyy", "repo": "PS-Game-Patch"},
    {"name": "ps-patch-system", "type": "github", "owner": "illusionyy", "repo": "ps-patch-system"},
    {"name": "ShadowMountPlus", "type": "github", "owner": "drakmor", "repo": "ShadowMountPlus"},
    {"name": "shsrv", "type": "github", "owner": "ps5-payload-dev", "repo": "shsrv"},
    {"name": "websrv", "type": "github", "owner": "ps5-payload-dev", "repo": "websrv"},
    {"name": "zftpd", "type": "github", "owner": "seregonwar", "repo": "zftpd"},
]


def load_sources():
    """Load SOURCES from sources.json. Falls back to built-in defaults.
    Always returns the list sorted alphabetically by name (case-insensitive).
    """
    path = _resource_path("sources.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list) or not data:
            raise ValueError("sources.json must be a non-empty JSON array")
        # light validation
        for i, s in enumerate(data):
            if not isinstance(s, dict) or "name" not in s or "type" not in s:
                raise ValueError(f"sources.json[{i}] missing required keys (name, type)")
    except FileNotFoundError:
        print(f"[warn] sources.json not found at {path} – using built-in defaults", file=sys.stderr)
        data = list(DEFAULT_SOURCES)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] failed to load sources.json ({e}) – using built-in defaults", file=sys.stderr)
        data = list(DEFAULT_SOURCES)

    # alphabetical by display name (case-insensitive)
    data.sort(key=lambda s: s.get("name", "").lower())
    return data


SOURCES = load_sources()

USER_AGENT = "ps5-payload-usb-builder/1.2"
VERSION = "1.2"

INTERESTING_EXTS = {".elf", ".bin", ".self", ".prx", ".js", ".xml", ".json"}

CHECK_OFF = "☐"
CHECK_ON  = "☑"

# ---------------------------------------------------------------------------
# Colour palette  (modern dark PS5-inspired)
# ---------------------------------------------------------------------------
C = {
    "bg":          "#0f1117",
    "bg2":         "#161b22",
    "bg3":         "#1c2333",
    "bg4":         "#242b3d",
    "fg":          "#e6edf3",
    "fg_dim":      "#8b949e",
    "fg_muted":    "#6e7681",
    "accent":      "#3d8bfd",       # bright blue
    "accent_h":    "#5ca0ff",
    "accent2":     "#7c5cff",       # purple accent
    "success":     "#3fb950",
    "warning":     "#d29922",
    "error":       "#f85149",
    "border":      "#30363d",
    "select":      "#1f6feb33",
    "tree_sel":    "#1f6feb",
    "header":      "#0d1117",
    "btn":         "#21262d",
    "btn_h":       "#30363d",
    "input_bg":    "#0d1117",
    "log_bg":      "#0a0c10",
}

# ---------------------------------------------------------------------------
# Networking helpers (worker threads only)
# ---------------------------------------------------------------------------

def http_get_json(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json, application/json",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get_bytes(url, progress_cb=None):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = resp.headers.get("Content-Length")
        total = int(total) if total else None
        data = io.BytesIO()
        read = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            data.write(chunk)
            read += len(chunk)
            if progress_cb:
                progress_cb(read, total)
        return data.getvalue()


def normalize_release(rel):
    assets = []
    for a in rel.get("assets", []) or []:
        assets.append({
            "name": a.get("name"),
            "size": a.get("size", 0),
            "url": a.get("browser_download_url") or a.get("url"),
        })
    return {
        "tag": rel.get("tag_name") or rel.get("name") or "unknown",
        "name": rel.get("name") or rel.get("tag_name") or "unknown",
        "prerelease": bool(rel.get("prerelease")),
        "draft": bool(rel.get("draft")),
        "assets": assets,
    }


def fetch_source_releases(source):
    if source["type"] == "github":
        url = f"https://api.github.com/repos/{source['owner']}/{source['repo']}/releases"
    elif source["type"] == "gitea":
        base = source["base"].rstrip("/")
        url = f"{base}/api/v1/repos/{source['owner']}/{source['repo']}/releases"
    else:
        raise ValueError(f"unknown source type {source['type']}")

    raw = http_get_json(url)
    releases = [normalize_release(r) for r in raw]
    releases = [r for r in releases if r["assets"]]

    if releases or not source.get("tags_fallback"):
        return releases

    tags_url = f"https://api.github.com/repos/{source['owner']}/{source['repo']}/tags"
    tags = http_get_json(tags_url)
    fallback = []
    for t in tags:
        fallback.append({
            "tag": t.get("name", "unknown"),
            "name": f"{t.get('name')} (source only, no binary release)",
            "prerelease": False,
            "draft": False,
            "assets": [{
                "name": f"{t.get('name')}-source.zip",
                "size": 0,
                "url": t.get("zipball_url"),
            }],
        })
    return fallback


# ---------------------------------------------------------------------------
# Tooltip helper
# ---------------------------------------------------------------------------

class Tooltip:
    def __init__(self, widget, text, delay=450):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._id = None
        self._tip = None
        widget.bind("<Enter>", self._schedule)
        widget.bind("<Leave>", self._hide)
        widget.bind("<ButtonPress>", self._hide)

    def _schedule(self, _=None):
        self._hide()
        self._id = self.widget.after(self.delay, self._show)

    def _show(self):
        if self._tip:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tw.configure(bg=C["border"])
        frame = tk.Frame(tw, bg=C["bg3"], padx=1, pady=1)
        frame.pack()
        lbl = tk.Label(frame, text=self.text, justify="left",
                       background=C["bg3"], foreground=C["fg"],
                       font=("Segoe UI", 9), padx=8, pady=5,
                       wraplength=280)
        lbl.pack()

    def _hide(self, _=None):
        if self._id:
            self.widget.after_cancel(self._id)
            self._id = None
        if self._tip:
            self._tip.destroy()
            self._tip = None


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"PS5 Payload USB Builder  ·  v{VERSION}")
        self.geometry("1240x740")
        self.minsize(980, 600)
        self.configure(bg=C["bg"])

        # try to set a nice window icon (optional, fails silently)
        try:
            self.iconphoto(False, tk.PhotoImage(data=_ICON_B64))
        except Exception:
            pass

        self.work_q = queue.Queue()
        self.node_data = {}
        self.checked = {}
        self.zip_cache = {}
        self.seq_items = []
        self._building = False

        self._setup_styles()
        self._build_ui()
        self.after(80, self._poll_queue)
        self.after(180, self.refresh_all)

        # Log where sources came from (helpful when frozen as .exe)
        src_path = _resource_path("sources.json")
        if os.path.isfile(src_path):
            self.after(200, lambda: self.log_msg(
                f"Loaded {len(SOURCES)} source(s) from {src_path}", "accent"))
        else:
            self.after(200, lambda: self.log_msg(
                f"sources.json not found – using {len(SOURCES)} built-in default(s)", "warn"))

    # -- styling -----------------------------------------------------------

    def _setup_styles(self):
        style = ttk.Style(self)
        # Prefer clam – most customisable
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        # Base
        style.configure(".",
                        background=C["bg"],
                        foreground=C["fg"],
                        fieldbackground=C["input_bg"],
                        borderwidth=0,
                        focuscolor=C["accent"],
                        font=("Segoe UI", 10))

        style.configure("TFrame", background=C["bg"])
        style.configure("Card.TFrame", background=C["bg2"])
        style.configure("Header.TFrame", background=C["header"])
        style.configure("Toolbar.TFrame", background=C["bg2"])

        style.configure("TLabel",
                        background=C["bg"],
                        foreground=C["fg"],
                        font=("Segoe UI", 10))
        style.configure("Header.TLabel",
                        background=C["header"],
                        foreground=C["fg"],
                        font=("Segoe UI Semibold", 14))
        style.configure("SubHeader.TLabel",
                        background=C["header"],
                        foreground=C["fg_dim"],
                        font=("Segoe UI", 9))
        style.configure("Dim.TLabel",
                        background=C["bg2"],
                        foreground=C["fg_dim"],
                        font=("Segoe UI", 9))
        style.configure("Muted.TLabel",
                        background=C["bg"],
                        foreground=C["fg_muted"],
                        font=("Segoe UI", 9))
        style.configure("CardTitle.TLabel",
                        background=C["bg2"],
                        foreground=C["fg"],
                        font=("Segoe UI Semibold", 11))
        style.configure("Status.TLabel",
                        background=C["bg3"],
                        foreground=C["fg_dim"],
                        font=("Segoe UI", 9))

        # Buttons
        style.configure("TButton",
                        background=C["btn"],
                        foreground=C["fg"],
                        borderwidth=0,
                        focusthickness=0,
                        padding=(12, 7),
                        font=("Segoe UI", 9))
        style.map("TButton",
                  background=[("active", C["btn_h"]), ("pressed", C["border"]),
                              ("disabled", C["bg3"])],
                  foreground=[("disabled", C["fg_muted"])])

        style.configure("Accent.TButton",
                        background=C["accent"],
                        foreground="#ffffff",
                        padding=(14, 8),
                        font=("Segoe UI Semibold", 10))
        style.map("Accent.TButton",
                  background=[("active", C["accent_h"]), ("pressed", "#2a6fd6"),
                              ("disabled", C["bg4"])],
                  foreground=[("disabled", C["fg_muted"])])

        style.configure("Success.TButton",
                        background=C["success"],
                        foreground="#ffffff",
                        padding=(14, 8),
                        font=("Segoe UI Semibold", 10))
        style.map("Success.TButton",
                  background=[("active", "#48c75c"), ("pressed", "#2ea043"),
                              ("disabled", C["bg4"])])

        style.configure("Ghost.TButton",
                        background=C["bg2"],
                        foreground=C["fg_dim"],
                        padding=(10, 5),
                        font=("Segoe UI", 9))
        style.map("Ghost.TButton",
                  background=[("active", C["bg3"])],
                  foreground=[("active", C["fg"])])

        # Entry
        style.configure("TEntry",
                        fieldbackground=C["input_bg"],
                        foreground=C["fg"],
                        insertcolor=C["fg"],
                        borderwidth=1,
                        padding=6)
        style.map("TEntry",
                  fieldbackground=[("focus", C["bg3"])],
                  bordercolor=[("focus", C["accent"])])

        # Treeview
        style.configure("Treeview",
                        background=C["bg2"],
                        foreground=C["fg"],
                        fieldbackground=C["bg2"],
                        borderwidth=0,
                        rowheight=26,
                        font=("Segoe UI", 10))
        style.configure("Treeview.Heading",
                        background=C["bg3"],
                        foreground=C["fg_dim"],
                        borderwidth=0,
                        relief="flat",
                        font=("Segoe UI Semibold", 9),
                        padding=(8, 6))
        style.map("Treeview",
                  background=[("selected", C["tree_sel"])],
                  foreground=[("selected", "#ffffff")])
        style.map("Treeview.Heading",
                  background=[("active", C["bg4"])])

        # Scrollbar
        style.configure("Vertical.TScrollbar",
                        background=C["bg3"],
                        troughcolor=C["bg"],
                        borderwidth=0,
                        arrowsize=12,
                        width=10)
        style.map("Vertical.TScrollbar",
                  background=[("active", C["bg4"]), ("pressed", C["border"])])

        # Progressbar
        style.configure("TProgressbar",
                        background=C["accent"],
                        troughcolor=C["bg3"],
                        borderwidth=0,
                        thickness=4)

        # Separator
        style.configure("TSeparator", background=C["border"])

        # Panedwindow sash
        style.configure("TPanedwindow", background=C["bg"])
        style.configure("Sash", sashthickness=6, sashpad=2)

    # -- UI construction ---------------------------------------------------

    def _build_ui(self):
        # ── Header bar ──────────────────────────────────────────────────
        header = ttk.Frame(self, style="Header.TFrame")
        header.pack(fill=tk.X)

        header_inner = ttk.Frame(header, style="Header.TFrame")
        header_inner.pack(fill=tk.X, padx=18, pady=(14, 12))

        left_h = ttk.Frame(header_inner, style="Header.TFrame")
        left_h.pack(side=tk.LEFT)

        ttk.Label(left_h, text="⚡  PS5 Payload USB Builder",
                  style="Header.TLabel").pack(anchor="w")
        ttk.Label(left_h, text="Select payloads → order them → build your payload USB folder",
                  style="SubHeader.TLabel").pack(anchor="w", pady=(2, 0))

        right_h = ttk.Frame(header_inner, style="Header.TFrame")
        right_h.pack(side=tk.RIGHT)
        ttk.Label(right_h, text=f"v{VERSION}", style="SubHeader.TLabel").pack(side=tk.RIGHT, padx=(0, 4))

        # thin accent line under header
        accent_line = tk.Frame(self, bg=C["accent"], height=2)
        accent_line.pack(fill=tk.X)

        # ── Main content ────────────────────────────────────────────────
        body = ttk.Frame(self)
        body.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        root_pane = ttk.Panedwindow(body, orient=tk.HORIZONTAL)
        root_pane.pack(fill=tk.BOTH, expand=True)

        # ── LEFT panel: sources tree ────────────────────────────────────
        left_card = ttk.Frame(root_pane, style="Card.TFrame")
        root_pane.add(left_card, weight=3)

        # card header
        left_hdr = ttk.Frame(left_card, style="Card.TFrame")
        left_hdr.pack(fill=tk.X, padx=12, pady=(12, 6))

        ttk.Label(left_hdr, text="📦  Payload Sources",
                  style="CardTitle.TLabel").pack(side=tk.LEFT)

        btn_row = ttk.Frame(left_hdr, style="Card.TFrame")
        btn_row.pack(side=tk.RIGHT)

        self.refresh_btn = ttk.Button(btn_row, text="↻  Refresh",
                                      command=self.refresh_all, style="Ghost.TButton")
        self.refresh_btn.pack(side=tk.LEFT, padx=(0, 6))
        Tooltip(self.refresh_btn, "Re-fetch release lists from all sources")

        self.add_btn = ttk.Button(btn_row, text="＋  Add Checked → Sequence",
                                  command=self.add_checked_to_sequence, style="Accent.TButton")
        self.add_btn.pack(side=tk.LEFT)
        Tooltip(self.add_btn, "Add all checked assets / zip members to the load sequence")

        # tip strip
        tip = ttk.Label(left_card,
                        text="  Tip: click a row to toggle checkbox · expand a .zip to inspect files inside",
                        style="Dim.TLabel")
        tip.pack(fill=tk.X, padx=12, pady=(0, 6))

        # tree + scrollbar
        tree_frame = ttk.Frame(left_card, style="Card.TFrame")
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        self.tree = ttk.Treeview(tree_frame, columns=("info",), show="tree headings",
                                 selectmode="none")
        self.tree.heading("#0", text="Source  /  Release  /  Asset", anchor="w")
        self.tree.heading("info", text="Info", anchor="w")
        self.tree.column("#0", width=440, minwidth=220)
        self.tree.column("info", width=200, minwidth=100)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<<TreeviewOpen>>", self._on_tree_open)

        for s in SOURCES:
            node = self.tree.insert("", "end", text=f"  {s['name']}", open=False,
                                    values=("loading…",))
            self.node_data[node] = {"kind": "source", "source": s}

        # ── RIGHT panel: sequence + build ───────────────────────────────
        right_card = ttk.Frame(root_pane, style="Card.TFrame")
        root_pane.add(right_card, weight=2)

        # sequence header
        seq_hdr = ttk.Frame(right_card, style="Card.TFrame")
        seq_hdr.pack(fill=tk.X, padx=12, pady=(12, 6))
        ttk.Label(seq_hdr, text="📋  Load Sequence",
                  style="CardTitle.TLabel").pack(side=tk.LEFT)
        self.seq_count_lbl = ttk.Label(seq_hdr, text="0 items", style="Dim.TLabel")
        self.seq_count_lbl.pack(side=tk.RIGHT)

        # sequence tree
        seq_frame = ttk.Frame(right_card, style="Card.TFrame")
        seq_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 4))

        self.seq_tree = ttk.Treeview(seq_frame, columns=("delay", "src"),
                                     show="tree headings", height=11, selectmode="browse")
        self.seq_tree.heading("#0", text="Payload filename", anchor="w")
        self.seq_tree.heading("delay", text="Delay (ms)", anchor="center")
        self.seq_tree.heading("src", text="From", anchor="w")
        self.seq_tree.column("#0", width=210, minwidth=120)
        self.seq_tree.column("delay", width=90, anchor="center", minwidth=70)
        self.seq_tree.column("src", width=130, minwidth=80)

        seq_vsb = ttk.Scrollbar(seq_frame, orient="vertical", command=self.seq_tree.yview)
        self.seq_tree.configure(yscrollcommand=seq_vsb.set)
        self.seq_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        seq_vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # sequence action buttons
        seq_btns = ttk.Frame(right_card, style="Card.TFrame")
        seq_btns.pack(fill=tk.X, padx=12, pady=(4, 8))

        for text, cmd, tip in [
            ("▲ Up",     lambda: self.move_seq(-1), "Move selected payload earlier"),
            ("▼ Down",   lambda: self.move_seq(1),  "Move selected payload later"),
            ("⏱ Delay",  self.edit_delay,           "Edit delay before this payload (ms)"),
            ("✕ Remove", self.remove_seq,           "Remove selected payload from sequence"),
        ]:
            b = ttk.Button(seq_btns, text=text, command=cmd, style="Ghost.TButton")
            b.pack(side=tk.LEFT, padx=(0, 5))
            Tooltip(b, tip)

        # divider
        ttk.Separator(right_card, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=12, pady=4)

        # output folder
        out_frame = ttk.Frame(right_card, style="Card.TFrame")
        out_frame.pack(fill=tk.X, padx=12, pady=(6, 4))
        ttk.Label(out_frame, text="Output folder  (payloads + autoload.txt go here)",
                  style="Dim.TLabel").pack(anchor="w")

        pick_row = ttk.Frame(out_frame, style="Card.TFrame")
        pick_row.pack(fill=tk.X, pady=(4, 0))
        self.outdir_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "ps5_payload_usb"))
        out_entry = ttk.Entry(pick_row, textvariable=self.outdir_var)
        out_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        browse_btn = ttk.Button(pick_row, text="Browse…", command=self.pick_outdir, style="Ghost.TButton")
        browse_btn.pack(side=tk.LEFT)
        Tooltip(browse_btn, "Choose where payloads and autoload.txt will be written")

        # big build button
        self.build_btn = ttk.Button(right_card,
                                    text="🚀  Build USB Folder",
                                    command=self.build_usb,
                                    style="Success.TButton")
        self.build_btn.pack(fill=tk.X, padx=12, pady=(10, 6))
        Tooltip(self.build_btn, "Download selected payloads and write autoload.txt")

        # progress bar (hidden until build)
        self.progress = ttk.Progressbar(right_card, mode="indeterminate", length=200)
        # packed on demand

        # log
        log_hdr = ttk.Frame(right_card, style="Card.TFrame")
        log_hdr.pack(fill=tk.X, padx=12, pady=(4, 2))
        ttk.Label(log_hdr, text="Log", style="Dim.TLabel").pack(side=tk.LEFT)
        clear_btn = ttk.Button(log_hdr, text="Clear", command=self._clear_log, style="Ghost.TButton")
        clear_btn.pack(side=tk.RIGHT)

        log_frame = tk.Frame(right_card, bg=C["border"], padx=1, pady=1)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        self.log = tk.Text(log_frame, height=8, wrap="word", state="disabled",
                           bg=C["log_bg"], fg=C["fg_dim"],
                           insertbackground=C["fg"],
                           font=("Cascadia Code", 9) if self._font_exists("Cascadia Code") else ("Consolas", 9),
                           relief="flat", padx=8, pady=6,
                           selectbackground=C["tree_sel"])
        self.log.pack(fill=tk.BOTH, expand=True)

        # coloured log tags
        self.log.tag_configure("info",    foreground=C["fg_dim"])
        self.log.tag_configure("ok",      foreground=C["success"])
        self.log.tag_configure("warn",    foreground=C["warning"])
        self.log.tag_configure("err",     foreground=C["error"])
        self.log.tag_configure("accent",  foreground=C["accent"])
        self.log.tag_configure("time",    foreground=C["fg_muted"])

        # ── Status bar ──────────────────────────────────────────────────
        status = ttk.Frame(self, style="Header.TFrame")
        status.pack(fill=tk.X, side=tk.BOTTOM)
        tk.Frame(status, bg=C["border"], height=1).pack(fill=tk.X)

        status_inner = ttk.Frame(status, style="Header.TFrame")
        status_inner.pack(fill=tk.X, padx=14, pady=6)

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(status_inner, textvariable=self.status_var, style="Status.TLabel").pack(side=tk.LEFT)

        self.src_status = ttk.Label(status_inner, text="", style="Status.TLabel")
        self.src_status.pack(side=tk.RIGHT)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _font_exists(name):
        try:
            from tkinter import font as tkfont
            return name in tkfont.families()
        except Exception:
            return False

    def _set_status(self, msg):
        self.status_var.set(msg)

    def _update_seq_count(self):
        n = len(self.seq_items)
        self.seq_count_lbl.configure(text=f"{n} item{'s' if n != 1 else ''}")

    # -- logging -----------------------------------------------------------

    def log_msg(self, msg, tag="info"):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"[{ts}] ", "time")
        self.log.insert("end", msg + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # -- queue polling -----------------------------------------------------

    def _poll_queue(self):
        try:
            while True:
                fn = self.work_q.get_nowait()
                fn()
        except queue.Empty:
            pass
        self.after(80, self._poll_queue)

    def run_bg(self, fn):
        threading.Thread(target=fn, daemon=True).start()

    # -- fetching releases -------------------------------------------------

    def refresh_all(self):
        self._set_status("Refreshing sources…")
        self.refresh_btn.configure(state="disabled")
        total = sum(1 for d in self.node_data.values() if d.get("kind") == "source")
        self._refresh_remaining = total
        for node, data in list(self.node_data.items()):
            if data.get("kind") == "source":
                self._fetch_source_node(node, data["source"])

    def _fetch_source_node(self, node, source):
        self.tree.set(node, "info", "loading…")
        self.tree.item(node, text=f"  ⏳  {source['name']}")

        def worker():
            try:
                releases = fetch_source_releases(source)
                err = None
            except Exception as e:  # noqa: BLE001
                releases, err = [], str(e)

            def apply():
                for child in self.tree.get_children(node):
                    self.tree.delete(child)
                if err:
                    self.tree.item(node, text=f"  ⚠  {source['name']}")
                    self.tree.set(node, "info", f"error")
                    self.log_msg(f"[{source['name']}] fetch failed: {err}", "err")
                else:
                    icon = "📦" if releases else "📭"
                    self.tree.item(node, text=f"  {icon}  {source['name']}")
                    self.tree.set(node, "info", f"{len(releases)} release(s)")
                    for rel in releases:
                        channel = ("draft" if rel["draft"] else
                                   "prerelease" if rel["prerelease"] else "release")
                        ch_icon = {"release": "🟢", "prerelease": "🟡", "draft": "🔴"}.get(channel, "⚪")
                        rnode = self.tree.insert(node, "end",
                                                 text=f"  {ch_icon}  {rel['tag']}  [{channel}]",
                                                 values=(f"{len(rel['assets'])} asset(s)",))
                        self.node_data[rnode] = {"kind": "release", "source": source, "release": rel}
                        for asset in rel["assets"]:
                            is_zip = asset["name"].lower().endswith(".zip")
                            a_icon = "🗜" if is_zip else self._ext_icon(asset["name"])
                            anode = self.tree.insert(rnode, "end",
                                                     text=f"  {CHECK_OFF}  {a_icon}  {asset['name']}",
                                                     values=(self._fmt_size(asset["size"]),))
                            self.node_data[anode] = {
                                "kind": "asset", "source": source, "release": rel,
                                "asset": asset, "is_zip": is_zip,
                            }
                            self.checked[anode] = False
                            if is_zip:
                                ph = self.tree.insert(anode, "end", text="     (expand to inspect zip)")
                                self.node_data[ph] = {"kind": "zip_placeholder"}

                self._refresh_remaining -= 1
                if self._refresh_remaining <= 0:
                    self.refresh_btn.configure(state="normal")
                    self._set_status("Ready")
                    self.src_status.configure(text=f"{len(SOURCES)} sources")
                    self.log_msg("All sources refreshed", "ok")

            self.work_q.put(apply)

        self.run_bg(worker)

    @staticmethod
    def _ext_icon(name):
        ext = os.path.splitext(name)[1].lower()
        return {
            ".elf": "⚙", ".bin": "⚙", ".self": "⚙", ".prx": "⚙",
            ".js": "📜", ".xml": "📄", ".json": "📄",
            ".zip": "🗜",
        }.get(ext, "📎")

    @staticmethod
    def _fmt_size(n):
        if not n:
            return ""
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.0f} {unit}"
            n /= 1024
        return f"{n:.1f} TB"

    # -- zip lazy expansion ------------------------------------------------

    def _on_tree_open(self, event):
        node = self.tree.focus()
        data = self.node_data.get(node)
        if not data or data.get("kind") != "asset" or not data.get("is_zip"):
            return
        children = self.tree.get_children(node)
        if len(children) != 1:
            return
        ph = children[0]
        if self.node_data.get(ph, {}).get("kind") != "zip_placeholder":
            return

        self.tree.set(node, "info", "downloading zip…")
        self._set_status(f"Inspecting zip: {data['asset']['name']}")

        def worker():
            asset = data["asset"]
            try:
                local_path = self.zip_cache.get(asset["url"])
                if not local_path:
                    raw = http_get_bytes(asset["url"])
                    fd, local_path = tempfile.mkstemp(suffix=".zip")
                    with os.fdopen(fd, "wb") as f:
                        f.write(raw)
                    self.zip_cache[asset["url"]] = local_path
                with zipfile.ZipFile(local_path) as zf:
                    names = [n for n in zf.namelist() if not n.endswith("/")]
                err = None
            except Exception as e:  # noqa: BLE001
                names, err = [], str(e)

            def apply():
                self.tree.delete(ph)
                if err:
                    self.tree.set(node, "info", "zip error")
                    self.log_msg(f"Failed to inspect zip {asset['name']}: {err}", "err")
                    self._set_status("Ready")
                    return
                self.tree.set(node, "info", f"{len(names)} file(s) in zip")
                for member in names:
                    base = member.rsplit("/", 1)[-1]
                    icon = self._ext_icon(base)
                    mnode = self.tree.insert(node, "end",
                                             text=f"  {CHECK_OFF}  {icon}  {base}",
                                             values=(member,))
                    self.node_data[mnode] = {
                        "kind": "zip_member", "source": data["source"], "release": data["release"],
                        "asset": asset, "member": member, "member_base": base,
                    }
                    self.checked[mnode] = False
                self._set_status("Ready")

            self.work_q.put(apply)

        self.run_bg(worker)

    # -- checkbox handling -------------------------------------------------

    def _on_tree_click(self, event):
        region = self.tree.identify("region", event.x, event.y)
        node = self.tree.identify_row(event.y)
        if not node:
            return
        data = self.node_data.get(node)
        if not data or data.get("kind") not in ("asset", "zip_member"):
            return
        # only toggle when clicking near the start of the row (checkbox area)
        # or anywhere on the row for simplicity
        cur = self.checked.get(node, False)
        self.checked[node] = not cur
        text = self.tree.item(node, "text")
        # replace the checkbox character
        if CHECK_ON in text or CHECK_OFF in text:
            mark = CHECK_ON if not cur else CHECK_OFF
            # keep the rest of the text after the old mark
            parts = text.split(CHECK_ON if cur else CHECK_OFF, 1)
            if len(parts) == 2:
                new_text = parts[0] + mark + parts[1]
            else:
                new_text = mark + text[1:]
            self.tree.item(node, text=new_text)

    # -- sequence list -----------------------------------------------------

    def add_checked_to_sequence(self):
        added = 0
        for node, is_checked in list(self.checked.items()):
            if not is_checked:
                continue
            data = self.node_data.get(node)
            if not data:
                continue
            if data["kind"] == "asset":
                filename = data["asset"]["name"]
                fromlabel = data["source"]["name"]
                item = {"kind": "asset", "source": data["source"], "asset": data["asset"],
                        "filename": filename}
            elif data["kind"] == "zip_member":
                filename = data["member_base"]
                fromlabel = f"{data['source']['name']} (zip)"
                item = {"kind": "zip_member", "source": data["source"], "asset": data["asset"],
                        "member": data["member"], "filename": filename}
            else:
                continue

            item["delay"] = 1000
            self.seq_items.append(item)
            icon = self._ext_icon(filename)
            self.seq_tree.insert("", "end", text=f"  {icon}  {filename}",
                                 values=(item["delay"], fromlabel))
            added += 1
            # uncheck
            self.checked[node] = False
            text = self.tree.item(node, "text")
            if CHECK_ON in text:
                self.tree.item(node, text=text.replace(CHECK_ON, CHECK_OFF, 1))

        self._update_seq_count()
        if added:
            self.log_msg(f"Added {added} item(s) to the load sequence", "ok")
            self._set_status(f"Added {added} payload(s)")
        else:
            self.log_msg("Nothing checked – select payloads first", "warn")

    def _seq_selected_index(self):
        sel = self.seq_tree.selection()
        if not sel:
            return None
        return self.seq_tree.index(sel[0])

    def move_seq(self, delta):
        i = self._seq_selected_index()
        if i is None:
            return
        j = i + delta
        if j < 0 or j >= len(self.seq_items):
            return
        self.seq_items[i], self.seq_items[j] = self.seq_items[j], self.seq_items[i]
        self._redraw_sequence()
        children = self.seq_tree.get_children()
        self.seq_tree.selection_set(children[j])
        self.seq_tree.see(children[j])

    def remove_seq(self):
        i = self._seq_selected_index()
        if i is None:
            return
        name = self.seq_items[i]["filename"]
        del self.seq_items[i]
        self._redraw_sequence()
        self._update_seq_count()
        self.log_msg(f"Removed “{name}” from sequence", "info")

    def edit_delay(self):
        i = self._seq_selected_index()
        if i is None:
            messagebox.showinfo("No selection", "Select a payload in the sequence first.")
            return
        item = self.seq_items[i]

        win = tk.Toplevel(self)
        win.title("Edit delay")
        win.configure(bg=C["bg2"])
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()

        # centre over parent
        win.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - 320) // 2
        y = self.winfo_rooty() + (self.winfo_height() - 160) // 2
        win.geometry(f"320x160+{x}+{y}")

        frm = tk.Frame(win, bg=C["bg2"], padx=20, pady=16)
        frm.pack(fill=tk.BOTH, expand=True)

        tk.Label(frm, text=f"Delay before loading",
                 bg=C["bg2"], fg=C["fg_dim"], font=("Segoe UI", 9)).pack(anchor="w")
        tk.Label(frm, text=item["filename"],
                 bg=C["bg2"], fg=C["fg"], font=("Segoe UI Semibold", 11)).pack(anchor="w", pady=(0, 10))

        var = tk.StringVar(value=str(item["delay"]))
        entry = ttk.Entry(frm, textvariable=var, width=12, font=("Segoe UI", 12))
        entry.pack(anchor="w")
        entry.focus_set()
        entry.select_range(0, "end")

        tk.Label(frm, text="milliseconds  (0 = no delay)",
                 bg=C["bg2"], fg=C["fg_muted"], font=("Segoe UI", 8)).pack(anchor="w", pady=(4, 12))

        def save():
            try:
                item["delay"] = max(0, int(var.get().strip()))
            except ValueError:
                messagebox.showerror("Invalid value", "Delay must be a whole number of milliseconds.", parent=win)
                return
            self._redraw_sequence()
            win.destroy()

        btn_row = tk.Frame(frm, bg=C["bg2"])
        btn_row.pack(fill=tk.X)
        ttk.Button(btn_row, text="Save", command=save, style="Accent.TButton").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel", command=win.destroy, style="Ghost.TButton").pack(side=tk.LEFT)
        win.bind("<Return>", lambda e: save())
        win.bind("<Escape>", lambda e: win.destroy())

    def _redraw_sequence(self):
        self.seq_tree.delete(*self.seq_tree.get_children())
        for item in self.seq_items:
            fromlabel = item["source"]["name"] + (" (zip)" if item["kind"] == "zip_member" else "")
            icon = self._ext_icon(item["filename"])
            self.seq_tree.insert("", "end", text=f"  {icon}  {item['filename']}",
                                 values=(item["delay"], fromlabel))
        self._update_seq_count()

    # -- build -------------------------------------------------------------

    def pick_outdir(self):
        d = filedialog.askdirectory(initialdir=self.outdir_var.get())
        if d:
            self.outdir_var.set(d)

    def build_usb(self):
        if self._building:
            return
        if not self.seq_items:
            messagebox.showwarning("Nothing to build",
                                   "Add at least one payload to the sequence first.")
            return
        outdir = self.outdir_var.get().strip()
        if not outdir:
            messagebox.showwarning("No output folder", "Choose an output folder first.")
            return

        os.makedirs(outdir, exist_ok=True)
        seq_snapshot = list(self.seq_items)
        self._building = True
        self.build_btn.configure(state="disabled", text="⏳  Building…")
        self.progress.pack(fill=tk.X, padx=12, pady=(0, 6))
        self.progress.start(12)
        self._set_status("Building payload folder…")
        self.log_msg(f"Building into {outdir} …", "accent")

        def worker():
            used_names = set()
            lines = []
            ok = True
            for item in seq_snapshot:
                try:
                    final_name = self._materialize(item, outdir, used_names)
                except Exception as e:  # noqa: BLE001
                    ok = False
                    self.work_q.put(lambda e=e, item=item: self.log_msg(
                        f"FAILED: {item['filename']}: {e}", "err"))
                    continue
                if item["delay"] > 0:
                    lines.append(f"!{item['delay']}")
                lines.append(final_name)
                self.work_q.put(lambda n=item["filename"]: self.log_msg(f"Downloaded: {n}", "ok"))

            autoload_path = os.path.join(outdir, "autoload.txt")
            with open(autoload_path, "w", newline="\n") as f:
                f.write("\n".join(lines) + "\n")

            def done():
                self.progress.stop()
                self.progress.pack_forget()
                self._building = False
                self.build_btn.configure(state="normal", text="🚀  Build USB Folder")
                self.log_msg(f"Wrote {autoload_path}", "accent")
                self._set_status("Build complete" if ok else "Build finished with errors")
                if ok:
                    messagebox.showinfo(
                        "Done  ✓",
                        f"Payload folder built at:\n\n{outdir}\n\n"
                        "Copy the contents of this folder to the root of your USB drive\n(or into whatever folder your payload manager expects)."
                    )
                else:
                    messagebox.showwarning(
                        "Finished with errors",
                        "Some payloads failed to download.\nCheck the log for details."
                    )

            self.work_q.put(done)

        self.run_bg(worker)

    def _materialize(self, item, outdir, used_names):
        name = self._unique_name(item["filename"], used_names)
        dest = os.path.join(outdir, name)

        if item["kind"] == "asset":
            data = http_get_bytes(item["asset"]["url"])
            with open(dest, "wb") as f:
                f.write(data)
            return name

        if item["kind"] == "zip_member":
            local_zip = self.zip_cache.get(item["asset"]["url"])
            if not local_zip:
                raw = http_get_bytes(item["asset"]["url"])
                fd, local_zip = tempfile.mkstemp(suffix=".zip")
                with os.fdopen(fd, "wb") as f:
                    f.write(raw)
                self.zip_cache[item["asset"]["url"]] = local_zip
            with zipfile.ZipFile(local_zip) as zf, zf.open(item["member"]) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            return name

        raise ValueError(f"unknown item kind {item['kind']}")

    @staticmethod
    def _unique_name(name, used_names):
        if name not in used_names:
            used_names.add(name)
            return name
        base, ext = os.path.splitext(name)
        n = 2
        while f"{base}_{n}{ext}" in used_names:
            n += 1
        new_name = f"{base}_{n}{ext}"
        used_names.add(new_name)
        return new_name


# tiny 16×16 lightning-bolt icon (base64 PNG) for the window
_ICON_B64 = """
iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAhElEQVQ4T2NkYGD4z0ABYBzVMKoB
BgYGBgYGBoZ//xn+MzAyMjL8Z2Bg+M/AwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA
wMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA
wMDAwMDAwMDAwABr/gf/0gY5nQAAAABJRU5ErkJggg==
"""

if __name__ == "__main__":
    App().mainloop()
