#!/usr/bin/env python3
"""OPL Covers - downloads artwork (covers, disc, spine...) for Open PS2 Loader.

Detects the USB drive holding your OPL games, reads each game's ID (from the
file name, from SYSTEM.CNF inside the ISO, or from ul.cfg) and downloads the
selected artwork into the drive's ART folder, using the names OPL expects.

Main source: OPL Manager art database backup on archive.org.
Fallback source (front cover only): github.com/xlenore/ps2-covers.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from urllib.parse import quote

import requests
from PIL import Image

APP_NAME = "OPL Covers"
APP_DIR = Path(__file__).resolve().parent
CACHE_DIR = APP_DIR / "cache"

ART_DB = "OPLM_ART_2023_11"
ART_BASE = f"https://archive.org/download/{ART_DB}/{ART_DB}.zip"
INDEX_FILE = CACHE_DIR / f"{ART_DB}_index.json.gz"
FALLBACK_COV = "https://raw.githubusercontent.com/xlenore/ps2-covers/main/covers/default/{}.jpg"

USER_AGENT = "opl-covers/1.0 (+python-requests)"
WORKERS = 4

# (OPL code, UI label, checked by default)
ART_TYPES = [
    ("COV", "Front cover", True),
    ("COV2", "Back cover", False),
    ("ICO", "Disc (icon)", True),
    ("LAB", "Spine", False),
    ("LGO", "Logo", False),
    ("BG", "Background", False),
    ("SCR", "Screenshots", False),
]

# OPL only uses SCR_00/SCR_01 and BG_00; anything beyond just wastes space.
MAX_NUMBERED = {"SCR": 2, "BG": 1}

GAME_ID_RE = re.compile(r"([A-Z]{4})[_-](\d{3})\.?(\d{2})", re.I)
ART_NAME_RE = re.compile(r"^[A-Z]{4}_\d{3}\.\d{2}_([A-Z0-9]+?)((?:_\d+)?)\.(\w+)$", re.I)
INDEX_LINE_RE = re.compile(re.escape(ART_DB) + r"\.zip/PS2%2F([^%\"]+)%2F([^\"/]+)\"")


# --------------------------------------------------------------------------
# Games and IDs
# --------------------------------------------------------------------------

def normalize_id(text: str) -> str | None:
    m = GAME_ID_RE.search(text or "")
    if not m:
        return None
    return f"{m.group(1).upper()}_{m.group(2)}.{m.group(3)}"


def id_from_iso(path: Path) -> str | None:
    """Reads SYSTEM.CNF from the root of an ISO9660 image and extracts the BOOT2 ID."""
    try:
        with open(path, "rb") as f:
            f.seek(16 * 2048)
            pvd = f.read(2048)
            if pvd[1:6] != b"CD001":
                return None
            root = pvd[156:190]
            lba = struct.unpack("<I", root[2:6])[0]
            size = struct.unpack("<I", root[10:14])[0]
            f.seek(lba * 2048)
            data = f.read(min(size, 64 * 2048))
            i = 0
            while i < len(data):
                length = data[i]
                if length == 0:  # rest of the sector is padding
                    i = (i // 2048 + 1) * 2048
                    continue
                name_len = data[i + 32]
                name = data[i + 33:i + 33 + name_len].upper()
                if name.startswith(b"SYSTEM.CNF"):
                    flba = struct.unpack("<I", data[i + 2:i + 6])[0]
                    fsize = struct.unpack("<I", data[i + 10:i + 14])[0]
                    f.seek(flba * 2048)
                    cnf = f.read(min(fsize, 4096)).decode("latin-1", "replace")
                    for line in cnf.splitlines():
                        if line.strip().upper().startswith("BOOT"):
                            return normalize_id(line)
                    return None
                i += length
    except (OSError, struct.error, IndexError):
        return None
    return None


@dataclass
class Game:
    title: str
    path: str
    fmt: str
    game_id: str | None
    id_source: str
    existing: set[str] = field(default_factory=set)


def scan_games(root: Path) -> list[Game]:
    games: list[Game] = []
    for folder in ("DVD", "CD"):
        d = find_child(root, folder)
        if not d:
            continue
        for p in sorted(d.iterdir(), key=lambda x: x.name.lower()):
            ext = p.suffix.lower()
            if not p.is_file() or ext not in (".iso", ".zso"):
                continue
            title = p.stem
            gid = normalize_id(p.name[:12])
            src = "file name"
            if gid:
                # old naming scheme: "SLUS_200.62.Game Name.iso"
                title = re.sub(r"^[A-Z]{4}[_-]\d{3}\.\d{2}\.", "", title, flags=re.I)
            elif ext == ".iso":
                gid = id_from_iso(p)
                src = "SYSTEM.CNF"
            if not gid:
                src = "not found"
            games.append(Game(title, str(p), f"{folder}/{ext[1:].upper()}", gid, src))

    ul = find_child(root, "ul.cfg")
    if ul and ul.is_file():
        raw = ul.read_bytes()
        for off in range(0, len(raw) - 63, 64):
            rec = raw[off:off + 64]
            title = rec[:32].split(b"\0")[0].decode("latin-1", "replace").strip()
            gid = normalize_id(rec[32:47].decode("latin-1", "replace"))
            games.append(Game(title or "(untitled)", str(ul), "UL", gid,
                              "ul.cfg" if gid else "not found"))
    refresh_existing(root, games)
    return games


def find_child(root: Path, name: str) -> Path | None:
    """Finds a file/folder case-insensitively (FAT32 drives come in every casing)."""
    p = root / name
    if p.exists():
        return p
    try:
        for c in root.iterdir():
            if c.name.lower() == name.lower():
                return c
    except OSError:
        pass
    return None


def art_dir(root: Path) -> Path:
    return find_child(root, "ART") or (root / "ART")


def refresh_existing(root: Path, games: list[Game]) -> None:
    d = art_dir(root)
    names = []
    if d.is_dir():
        names = [n.upper() for n in os.listdir(d)]
    for g in games:
        g.existing = set()
        if not g.game_id:
            continue
        prefix = g.game_id.upper() + "_"
        for n in names:
            if n.startswith(prefix):
                m = ART_NAME_RE.match(n)
                if m:
                    g.existing.add(m.group(1).upper())


# --------------------------------------------------------------------------
# USB drives
# --------------------------------------------------------------------------

@dataclass
class Drive:
    mount: str
    label: str
    size: str
    fstype: str
    device: str
    has_opl: bool

    def describe(self) -> str:
        name = self.label or self.device
        tag = "  [OPL]" if self.has_opl else ""
        return f"{name} ({self.size}, {self.fstype}) at {self.mount}{tag}"


def looks_like_opl(path: Path) -> bool:
    return any(find_child(path, n) for n in ("DVD", "CD", "ul.cfg", "ART"))


def detect_drives() -> list[Drive]:
    drives: list[Drive] = []
    try:
        out = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,MOUNTPOINT,RM,TRAN,LABEL,SIZE,FSTYPE,HOTPLUG"],
            capture_output=True, text=True, timeout=10, check=True).stdout
        tree = json.loads(out)["blockdevices"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        tree = []

    def truthy(v) -> bool:
        return v in (True, 1, "1", "true")

    def walk(nodes, removable_parent=False):
        for n in nodes:
            removable = removable_parent or truthy(n.get("rm")) or truthy(n.get("hotplug")) \
                or n.get("tran") == "usb"
            mp = n.get("mountpoint")
            if removable and mp and mp not in ("/", "[SWAP]") and os.path.isdir(mp):
                drives.append(Drive(mp, n.get("label") or "", n.get("size") or "?",
                                    n.get("fstype") or "?", "/dev/" + n["name"],
                                    looks_like_opl(Path(mp))))
            walk(n.get("children") or [], removable)

    walk(tree)
    drives.sort(key=lambda d: not d.has_opl)
    return drives


# --------------------------------------------------------------------------
# Art database
# --------------------------------------------------------------------------

class ArtIndex:
    """Maps game ID -> list of (folder, file) inside the OPL Manager zip."""

    def __init__(self):
        self.data: dict[str, list[list[str]]] = {}
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    @property
    def loaded(self) -> bool:
        return bool(self.data)

    def load(self, log, progress) -> None:
        if INDEX_FILE.exists():
            with gzip.open(INDEX_FILE, "rt", encoding="utf-8") as f:
                self.data = json.load(f)
            log(f"Art index loaded from cache ({len(self.data)} games).")
            return
        log("Downloading the art database index (first run only, ~20 MB)...")
        data: dict[str, list[list[str]]] = {}
        got = 0
        with self.session.get(ART_BASE + "/", stream=True, timeout=(15, 120)) as r:
            r.raise_for_status()
            for chunk in r.iter_lines(decode_unicode=True):
                got += len(chunk) + 1
                progress(min(got / 20_000_000, 0.99))
                for folder, fname in INDEX_LINE_RE.findall(chunk or ""):
                    gid = normalize_id(folder)
                    if gid:
                        data.setdefault(gid, []).append([folder, fname])
        if not data:
            raise RuntimeError("The index came back empty. archive.org may be down.")
        CACHE_DIR.mkdir(exist_ok=True)
        tmp = INDEX_FILE.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(data, f)
        tmp.replace(INDEX_FILE)
        self.data = data
        progress(1.0)
        log(f"Index cached ({len(data)} games).")

    def entries(self, gid: str, types: set[str]) -> list[tuple[str, str, str]]:
        """Returns (type, url, target_name) for the art available for that game."""
        out = []
        for folder, fname in self.data.get(gid, []):
            m = ART_NAME_RE.match(fname)
            if not m:
                continue
            kind, num, ext = m.group(1).upper(), m.group(2), m.group(3).lower()
            if kind not in types:
                continue
            if num and int(num[1:]) >= MAX_NUMBERED.get(kind, 99):
                continue
            url = f"{ART_BASE}/PS2%2F{quote(folder)}%2F{quote(fname)}"
            out.append((kind, url, f"{gid}_{kind}{num}.{ext}"))
        return out


def fetch(session: requests.Session, url: str) -> bytes | None:
    for attempt in range(3):
        try:
            r = session.get(url, timeout=(15, 60))
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.content
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))
    return None


def fallback_cover(session: requests.Session, gid: str) -> bytes | None:
    raw = fetch(session, FALLBACK_COV.format(gid.replace("_", "-").replace(".", "")))
    if not raw:
        return None
    # OPL gets slow/unstable with large images: shrink to the standard size.
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    img = img.resize((140, 200), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def png_name(name: str) -> str:
    return name.rsplit(".", 1)[0] + ".png"


def to_png(data: bytes) -> bytes:
    img = Image.open(io.BytesIO(data))
    img = img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB")
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".part")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("980x700")
        self.minsize(820, 560)

        self.root_path: Path | None = None
        self.drives: list[Drive] = []
        self.games: list[Game] = []
        self.index = ArtIndex()
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.cancel = threading.Event()

        self.type_vars = {code: tk.BooleanVar(value=default) for code, _, default in ART_TYPES}
        self.overwrite = tk.BooleanVar(value=False)
        self.use_fallback = tk.BooleanVar(value=True)
        self.as_png = tk.BooleanVar(value=True)

        self._style()
        self._build()
        self.after(100, self._poll)
        self.after(300, self.auto_detect)

    # ---- layout ----------------------------------------------------------

    def _style(self):
        s = ttk.Style(self)
        if "clam" in s.theme_names():
            s.theme_use("clam")
        s.configure("Title.TLabel", font=("TkDefaultFont", 15, "bold"))
        s.configure("Sub.TLabel", foreground="#666")
        s.configure("Big.TButton", font=("TkDefaultFont", 11, "bold"), padding=8)
        s.configure("Treeview", rowheight=24)

    def _build(self):
        pad = {"padx": 10, "pady": 6}

        head = ttk.Frame(self)
        head.pack(fill="x", **pad)
        ttk.Label(head, text="OPL Covers", style="Title.TLabel").pack(side="left")
        ttk.Label(head, text="  artwork for Open PS2 Loader", style="Sub.TLabel").pack(side="left")

        # USB drive
        box = ttk.LabelFrame(self, text=" 1. USB drive ")
        box.pack(fill="x", **pad)
        self.drive_combo = ttk.Combobox(box, state="readonly")
        self.drive_combo.pack(side="left", fill="x", expand=True, padx=8, pady=8)
        self.drive_combo.bind("<<ComboboxSelected>>", lambda e: self.select_drive_from_combo())
        ttk.Button(box, text="Detect", command=self.auto_detect).pack(side="left", padx=4)
        ttk.Button(box, text="Choose folder...", command=self.pick_folder).pack(side="left", padx=(4, 8))
        self.drive_status = ttk.Label(self, text="No USB drive selected.", style="Sub.TLabel")
        self.drive_status.pack(fill="x", padx=14)

        # Art types
        types = ttk.LabelFrame(self, text=" 2. What to download ")
        types.pack(fill="x", **pad)
        row = ttk.Frame(types)
        row.pack(fill="x", padx=8, pady=(8, 2))
        for n, (code, label, _) in enumerate(ART_TYPES):
            ttk.Checkbutton(row, text=f"{label} ({code})", variable=self.type_vars[code]).grid(
                row=n // 4, column=n % 4, sticky="w", padx=(0, 18), pady=1)
        row2 = ttk.Frame(types)
        row2.pack(fill="x", padx=8, pady=(2, 8))
        ttk.Checkbutton(row2, text="Replace existing art",
                        variable=self.overwrite).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(row2, text="If no front cover is found, try the fallback source (xlenore)",
                        variable=self.use_fallback).pack(side="left")
        row3 = ttk.Frame(types)
        row3.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Checkbutton(row3, text="Save everything as PNG (use it if .jpg art doesn't show up in OPL)",
                        variable=self.as_png).pack(side="left")

        # Games
        gbox = ttk.LabelFrame(self, text=" 3. Games (double-click to edit the ID) ")
        gbox.pack(fill="both", expand=True, **pad)
        cols = ("title", "id", "fmt", "have")
        self.tree = ttk.Treeview(gbox, columns=cols, show="headings", selectmode="extended")
        for c, text, w, anchor in (("title", "Game", 420, "w"), ("id", "ID", 120, "center"),
                                   ("fmt", "Format", 90, "center"),
                                   ("have", "Art on drive", 220, "w")):
            self.tree.heading(c, text=text)
            self.tree.column(c, width=w, anchor=anchor, stretch=(c == "title"))
        self.tree.tag_configure("noid", foreground="#b00020")
        self.tree.tag_configure("done", foreground="#1b7f3a")
        sb = ttk.Scrollbar(gbox, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        sb.pack(side="left", fill="y", pady=8, padx=(0, 8))
        self.tree.bind("<Double-1>", self.edit_id)

        sel = ttk.Frame(self)
        sel.pack(fill="x", padx=10)
        ttk.Button(sel, text="Select all", command=self.select_all).pack(side="left")
        ttk.Button(sel, text="Clear selection",
                   command=lambda: self.tree.selection_remove(self.tree.selection())).pack(side="left", padx=6)
        ttk.Button(sel, text="Only missing covers", command=self.select_missing).pack(side="left")
        self.count_lbl = ttk.Label(sel, text="", style="Sub.TLabel")
        self.count_lbl.pack(side="left", padx=12)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_count())

        # Actions + progress
        act = ttk.Frame(self)
        act.pack(fill="x", **pad)
        self.go_btn = ttk.Button(act, text="Download art", style="Big.TButton", command=self.start_download)
        self.go_btn.pack(side="right")
        self.cancel_btn = ttk.Button(act, text="Cancel", command=self.cancel.set, state="disabled")
        self.cancel_btn.pack(side="right", padx=6)
        self.progress = ttk.Progressbar(act, mode="determinate", maximum=1.0)
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 10))

        self.log_box = tk.Text(self, height=7, state="disabled", wrap="word",
                               font=("TkFixedFont", 9), relief="flat", background="#f4f4f4")
        self.log_box.pack(fill="x", padx=10, pady=(0, 10))

    # ---- helpers ---------------------------------------------------------

    def log(self, msg: str):
        self.events.put(("log", msg))

    def _log_now(self, msg: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", time.strftime("%H:%M:%S ") + msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._log_now(payload)
                elif kind == "progress":
                    self.progress["value"] = payload
                elif kind == "done":
                    self._finish(payload)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _set_busy(self, busy: bool):
        self.busy = busy
        self.go_btn.configure(state="disabled" if busy else "normal")
        self.cancel_btn.configure(state="normal" if busy else "disabled")

    # ---- USB drive -------------------------------------------------------

    def auto_detect(self):
        if self.busy:
            return
        self.drives = detect_drives()
        self.drive_combo["values"] = [d.describe() for d in self.drives]
        if not self.drives:
            self.drive_combo.set("")
            self.drive_status.configure(text="No USB drive found. Plug one in and click Detect, "
                                             "or use 'Choose folder...'.")
            self._log_now("No mounted USB drive was found.")
            return
        d = self.drives[0]
        self.drive_combo.current(0)
        extra = ""
        if len(self.drives) > 1:
            extra = f"\n\n(Found {len(self.drives)} devices; you can switch in the list.)"
        warn = "" if d.has_opl else "\n\nWarning: no OPL DVD/CD/ART folders found on it."
        ok = messagebox.askyesno(
            "USB drive detected",
            f"Detected this USB drive:\n\n{d.label or d.device}\n"
            f"Size: {d.size}   Filesystem: {d.fstype}\nMounted at: {d.mount}{warn}{extra}\n\n"
            "Use this drive?", parent=self)
        if ok:
            self.use_root(Path(d.mount), confirmed=True)
        else:
            self.drive_status.configure(text="Pick the drive from the list above or choose a folder.")

    def select_drive_from_combo(self):
        i = self.drive_combo.current()
        if 0 <= i < len(self.drives):
            d = self.drives[i]
            if messagebox.askyesno("Confirm drive",
                                   f"Use {d.label or d.device} ({d.size}) at {d.mount}?", parent=self):
                self.use_root(Path(d.mount), confirmed=True)

    def pick_folder(self):
        p = filedialog.askdirectory(title="OPL root folder (where DVD, CD, ART live)")
        if p:
            self.use_root(Path(p))

    def use_root(self, path: Path, confirmed: bool = False):
        if not confirmed and not looks_like_opl(path):
            if not messagebox.askyesno("Not an OPL folder",
                                       f"{path} has no DVD, CD, ART or ul.cfg.\nUse it anyway?"):
                return
        self.root_path = path
        free = shutil.disk_usage(path).free / 1024 ** 3
        self.drive_status.configure(text=f"Using: {path}   •   free: {free:.1f} GB   •   "
                                         f"art goes to {art_dir(path)}")
        self.load_games()

    def load_games(self):
        if not self.root_path:
            return
        self._log_now(f"Reading games from {self.root_path} ...")
        self.games = scan_games(self.root_path)
        self._fill_tree()
        noid = sum(1 for g in self.games if not g.game_id)
        self._log_now(f"{len(self.games)} game(s) found" +
                      (f", {noid} without an ID (double-click to enter it)." if noid else "."))
        self.select_all()

    def _fill_tree(self):
        sel = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        labels = {c: l for c, l, _ in ART_TYPES}
        for i, g in enumerate(self.games):
            have = ", ".join(labels[c].split(" (")[0] for c, _, _ in ART_TYPES if c in g.existing) or "—"
            tag = "noid" if not g.game_id else ("done" if "COV" in g.existing else "")
            self.tree.insert("", "end", iid=str(i), tags=(tag,),
                             values=(g.title, g.game_id or "???", g.fmt, have))
        keep = [s for s in sel if self.tree.exists(s)]
        if keep:
            self.tree.selection_set(keep)
        self._update_count()

    def _update_count(self):
        self.count_lbl.configure(text=f"{len(self.tree.selection())} of {len(self.games)} selected")

    def select_all(self):
        self.tree.selection_set(self.tree.get_children())

    def select_missing(self):
        self.tree.selection_set([str(i) for i, g in enumerate(self.games) if "COV" not in g.existing])

    def edit_id(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid or self.busy:
            return
        g = self.games[int(iid)]
        val = simpledialog.askstring("Game ID",
                                     f"{g.title}\n\nID (e.g. SLUS_200.62 — printed on the spine/disc):",
                                     initialvalue=g.game_id or "", parent=self)
        if val is None:
            return
        gid = normalize_id(val)
        if not gid:
            messagebox.showerror("Invalid ID", "Use the format SLUS_200.62 (or SLUS-20062).")
            return
        g.game_id, g.id_source = gid, "manual"
        refresh_existing(self.root_path, self.games)
        self._fill_tree()

    # ---- download --------------------------------------------------------

    def start_download(self):
        if self.busy:
            return
        if not self.root_path:
            messagebox.showwarning(APP_NAME, "Choose the USB drive first.")
            return
        types = {c for c, v in self.type_vars.items() if v.get()}
        if not types:
            messagebox.showwarning(APP_NAME, "Check at least one art type.")
            return
        chosen = [self.games[int(i)] for i in self.tree.selection()]
        chosen = [g for g in chosen if g.game_id]
        if not chosen:
            messagebox.showwarning(APP_NAME, "Select games that have an ID.")
            return
        labels = ", ".join(l for c, l, _ in ART_TYPES if c in types)
        if not messagebox.askyesno(
                "Confirm download",
                f"Download [{labels}] for {len(chosen)} game(s)?\n\n"
                f"Destination: {art_dir(self.root_path)}\n"
                f"Existing art: {'will be replaced' if self.overwrite.get() else 'will be kept'}"):
            return
        self.cancel.clear()
        self._set_busy(True)
        self.progress["value"] = 0
        threading.Thread(target=self._worker, daemon=True,
                         args=(chosen, types, self.overwrite.get(), self.use_fallback.get(),
                               self.as_png.get(), self.root_path)).start()

    def _worker(self, games, types, overwrite, use_fallback, as_png, root: Path):
        stats = {"ok": 0, "skip": 0, "missing": 0, "err": 0}
        try:
            if not self.index.loaded:
                self.index.load(self.log, lambda v: self.events.put(("progress", v)))
            dest = art_dir(root)
            dest.mkdir(exist_ok=True)
            existing = {n.upper() for n in os.listdir(dest)}

            jobs = []  # (game, kind, url|None, name)
            for g in games:
                found = self.index.entries(g.game_id, types)
                if as_png:
                    found = [(k, u, png_name(n)) for k, u, n in found]
                kinds_found = {k for k, _, _ in found}
                for kind, url, name in found:
                    if not overwrite and name.upper() in existing:
                        stats["skip"] += 1
                        self.log(f"- {g.title}: {name} already exists, kept "
                                 "(check 'Replace' to swap it).")
                        continue
                    jobs.append((g, kind, url, name))
                if "COV" in types and "COV" not in kinds_found:
                    name = f"{g.game_id}_COV.{'png' if as_png else 'jpg'}"
                    if not overwrite and name.upper() in existing:
                        stats["skip"] += 1
                        self.log(f"- {g.title}: front cover already exists, kept.")
                    elif use_fallback:
                        jobs.append((g, "COV", None, name))
                    else:
                        stats["missing"] += 1
                        self.log(f"✗ {g.title}: front cover not found.")
                for kind in types - kinds_found - {"COV"}:
                    stats["missing"] += 1
                if not found and not ("COV" in types and use_fallback):
                    self.log(f"✗ {g.title} ({g.game_id}): no art in the database.")

            self.log(f"{len(jobs)} file(s) to download, {stats['skip']} already present.")
            if not jobs:
                self.events.put(("progress", 1.0))
                return

            def run(job):
                g, kind, url, name = job
                if self.cancel.is_set():
                    return job, "cancel", None
                if url:
                    data = fetch(self.index.session, url)
                else:
                    data = fallback_cover(self.index.session, g.game_id)
                if not data:
                    return job, "missing", None
                if name.lower().endswith(".png") and not data.startswith(b"\x89PNG"):
                    data = to_png(data)
                write_atomic(dest / name, data)
                # Never keep two versions (jpg and png) of the same art: OPL would only pick one.
                stem = name.rsplit(".", 1)[0].upper()
                for other in os.listdir(dest):
                    o_stem, _, o_ext = other.rpartition(".")
                    if o_stem.upper() == stem and other != name and o_ext.lower() in ("jpg", "jpeg", "png", "bmp"):
                        (dest / other).unlink()
                return job, "ok", len(data)

            done = 0
            with ThreadPoolExecutor(WORKERS) as pool:
                futs = [pool.submit(run, j) for j in jobs]
                for fut in as_completed(futs):
                    done += 1
                    self.events.put(("progress", done / len(jobs)))
                    try:
                        (g, kind, url, name), status, size = fut.result()
                    except Exception as e:  # noqa: BLE001 - surface any failure in the log
                        stats["err"] += 1
                        self.log(f"! error: {e}")
                        continue
                    if status == "ok":
                        stats["ok"] += 1
                        src = "" if url else " (fallback source)"
                        self.log(f"✓ {g.title}: {name}{src}")
                    elif status == "missing":
                        stats["missing"] += 1
                        self.log(f"✗ {g.title}: {kind} not found.")
            if self.cancel.is_set():
                self.log("Cancelled.")
        except Exception as e:  # noqa: BLE001
            stats["err"] += 1
            self.log(f"! Failure: {e}")
        finally:
            self.events.put(("done", stats))

    def _finish(self, stats):
        self._set_busy(False)
        if self.root_path:
            refresh_existing(self.root_path, self.games)
            self._fill_tree()
        msg = (f"Downloaded: {stats['ok']}\nAlready present: {stats['skip']}\n"
               f"Not found: {stats['missing']}\nErrors: {stats['err']}")
        self._log_now("Done. " + msg.replace("\n", " | "))
        if stats["ok"]:
            msg += "\n\nRemember to safely eject the drive before unplugging it."
        messagebox.showinfo("Done", msg, parent=self)


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        print("Usage: ./run.sh            (opens the GUI)")
        print("       ./run.sh --reset    (clears the art index cache)")
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--reset":
        shutil.rmtree(CACHE_DIR, ignore_errors=True)
        print("Cache cleared.")
        return
    App().mainloop()


if __name__ == "__main__":
    main()
