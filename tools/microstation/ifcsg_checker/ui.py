"""Tkinter interface for the IFC+SG checker.

The window is attached to MicroStation as a tool setting and driven by
PyCadInputQueue.PythonMainLoop(), so MicroStation stays fully interactive while
the report is open. Selecting a row selects the matching geometry, so an object
needing review can be found without cross-referencing an exported spreadsheet.
"""

import builtins
import gc
import json
import os
import queue
import re
import threading
import traceback
import webbrowser

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    from tkinter import font as tkfont
    HAS_TK = True
except ImportError:
    HAS_TK = False

try:
    from MSPyMstnPlatform import PyCadInputQueue
    IN_MICROSTATION = True
except Exception:
    PyCadInputQueue = None
    IN_MICROSTATION = False

from . import bcf as bcf_mod
from . import preview as preview_mod
from . import report as report_mod
from . import spf
from . import sources as src_mod
from .library import Library
from .locate import Locator
from .rules import Engine, FAIL, WARN, UNKNOWN, NOT_APPLICABLE, PASS
from .spf import IfcFile

try:
    from . import TOOL_NAME
except ImportError:
    TOOL_NAME = "IFC + SG Checker"

PALETTE = {
    "bg": "#f3f5f8",
    "surface": "#ffffff",
    "border": "#dde2ea",
    "text": "#1b2330",
    "muted": "#667085",
    "accent": "#0b6bcb",
    "accent_hover": "#0a5bb0",
    "accent_pressed": "#084a91",
    "accent_disabled": "#9cc3ea",
    "header": "#0f1b2d",
    "header_text": "#ffffff",
    "header_muted": "#9fb0c8",
    "heading": "#eef1f5",
    "hover": "#eef2f7",
    "select": "#dcebfb",
    "banner": "#fff4e0",
    "banner_text": "#7a4b00",
}

STATUS_COLOUR = {
    FAIL: "#c4314b",
    WARN: "#b26a00",
    UNKNOWN: "#6b4fbb",
    NOT_APPLICABLE: "#7b8494",
    PASS: "#13804a",
}

STATUS_TINT = {
    FAIL: "#fdecef",
    WARN: "#fff4e0",
    UNKNOWN: "#f1edfb",
    NOT_APPLICABLE: "#f1f3f6",
    PASS: "#e8f6ee",
}

STATUS_GLYPH = {
    FAIL: "FAIL", WARN: "WARN", UNKNOWN: "UNKNOWN",
    NOT_APPLICABLE: "N/A", PASS: "PASS",
}

UI_FONT = "Segoe UI"
UI_FONT_BOLD = "Segoe UI Semibold"

# Rule categories that judge the file as a whole and so have no object.
FILE_LEVEL_CATEGORIES = {"schema", "georeferencing", "coordination", "units",
                         "hygiene", "governance"}

MAX_ROWS_PER_RULE = 2000
MAX_VISIBLE_FINDINGS = 5000
FILTER_DELAY_MS = 180
SINGLETON_NAME = "_ifcsg_checker_window"

SETTINGS_PATH = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
    "IFCSG_Checker", "window.json")


def _contains(needle, haystack):
    return not needle or needle in (haystack or "")


def _finding_matches(finding, want):
    return (_contains(want["entity"], str(finding.entity or "").lower())
            and _contains(want["name"], str(finding.name or "").lower())
            and _contains(want["status"], str(finding.severity or "").lower())
            and _contains(want["message"],
                          (str(finding.message or "") + " "
                           + str(finding.detail or "")).lower()))


def _load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_settings(values):
    try:
        folder = os.path.dirname(SETTINGS_PATH)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
            json.dump(values, fh)
    except Exception:
        pass


def _update_settings(**values):
    current = _load_settings()
    if not isinstance(current, dict):
        current = {}
    current.update(values)
    _save_settings(current)


def _available_physical_memory():
    if os.name != "nt":
        return None
    try:
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullAvailPhys)
    except Exception:
        return None
    return None


def _geometry_on_screen(geometry, window):
    """Reject a saved position that would land off the current desktop."""
    try:
        match = re.fullmatch(r"(\d+)x(\d+)([+-]\d+)?([+-]\d+)?", geometry or "")
        if match is None:
            return False
        width, height = int(match.group(1)), int(match.group(2))
        x = int(match.group(3)) if match.group(3) else None
        y = int(match.group(4)) if match.group(4) else None
        if width < 700 or height < 450:
            return False
        if x is None:
            return True
        return (-50 <= x <= window.winfo_screenwidth() - 200
                and -50 <= y <= window.winfo_screenheight() - 150)
    except Exception:
        return False


class MstnTk(tk.Tk):
    """A Tk window that co-operates with MicroStation's message loop.

    Follows Bentley's LineCreatorWithToolSettingDialog sample: hand the frame
    window to AttachTkinterToolSetting, then alternate Tk update() with
    PyCadInputQueue.PythonMainLoop() instead of calling mainloop(), which would
    block MicroStation for as long as the window stayed open.

    Attaching reparents the window into a MicroStation tool-setting frame, which
    strips the maximise box and the sizing border. Both are put back afterwards.
    """

    windows = []

    def __init__(self, dock=True):
        tk.Tk.__init__(self)
        self.withdraw()                       # stay hidden until the layout exists
        self.update_idletasks()
        self._frame_hwnd = None
        if dock:
            self._attach()
        self.resizable(True, True)
        self._restore_window_chrome()
        MstnTk.windows.append(self)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<F11>", self._toggle_maximise)

    def _attach(self):
        if not IN_MICROSTATION:
            return
        try:
            import win32gui
            frame = win32gui.GetParent(self.winfo_id())
            if frame:
                PyCadInputQueue.AttachTkinterToolSetting(frame)
                self._frame_hwnd = frame
        except Exception:
            pass          # an unattached window still works, it just floats free

    def _restore_window_chrome(self):
        """Put back the maximise box, minimise box and sizing border."""
        # AttachTkinterToolSetting owns the frame when docked. Rewriting the style
        # of a MicroStation-owned window can destabilise the host; only floating
        # Tk windows are safe to restyle.
        if self._frame_hwnd is not None:
            return
        try:
            import win32con
            import win32gui
            hwnd = self._frame_hwnd or win32gui.GetParent(self.winfo_id()) or self.winfo_id()
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
            style |= (win32con.WS_MAXIMIZEBOX | win32con.WS_MINIMIZEBOX
                      | win32con.WS_THICKFRAME | win32con.WS_SYSMENU | win32con.WS_CAPTION)
            win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, style)
            win32gui.SetWindowPos(
                hwnd, 0, 0, 0, 0, 0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOZORDER
                | win32con.SWP_FRAMECHANGED)
        except Exception:
            pass

    def present(self):
        """Show the window, on top and not minimised."""
        try:
            self.deiconify()
            self.state("normal")
            self.lift()
            self.attributes("-topmost", True)
            self.after(300, lambda: self._drop_topmost())
            self.focus_set()
        except Exception:
            pass

    def _drop_topmost(self):
        try:
            if not getattr(self, "keep_on_top", None) or not self.keep_on_top.get():
                self.attributes("-topmost", False)
        except Exception:
            pass

    def _toggle_maximise(self, _event=None):
        try:
            self.state("normal" if self.state() == "zoomed" else "zoomed")
        except Exception:
            pass
        return "break"

    def close(self):
        if self in MstnTk.windows:
            MstnTk.windows.remove(self)
        try:
            self.destroy()
        except Exception:
            pass

    @staticmethod
    def run():
        if not IN_MICROSTATION:
            if MstnTk.windows:
                MstnTk.windows[0].mainloop()
            return
        while MstnTk.windows:
            for window in list(MstnTk.windows):
                try:
                    window.update()
                except tk.TclError:
                    if window in MstnTk.windows:
                        MstnTk.windows.remove(window)
            try:
                PyCadInputQueue.PythonMainLoop()
            except Exception as exc:
                for window in list(MstnTk.windows):
                    try:
                        window.set_status("MicroStation message loop stopped: %s" % exc)
                        window._close_now()
                    except Exception:
                        pass
                break


class CheckerWindow(MstnTk):
    def __init__(self, library_root=None, dock=True, cop=None):
        self.library_root = library_root
        self.library = self._initial_library(library_root, cop)
        self.engine = Engine(self.library)
        self.editions = []
        self.sources = []
        self.source = None
        self.ifc = None
        self.report = None
        self.locator = None
        self.row_data = {}
        self.busy = False
        self.last_html = None
        self._worker = None
        self._worker_queue = queue.Queue()
        self._worker_callback = None
        self._worker_error_callback = None
        self._worker_cancel_callback = None
        self._cancel_event = None
        self._poll_job = None
        self._closing = False
        self._filter_jobs = {}

        MstnTk.__init__(self, dock=dock)
        self.title(TOOL_NAME)
        self.minsize(760, 500)
        self.configure(background=PALETTE["bg"])
        self.keep_on_top = tk.BooleanVar(value=False)
        self._apply_theme()
        self._build()
        self._apply_initial_geometry()
        self.bind("<Configure>", self._on_resize)
        self.present()
        self.refresh_sources()

    @staticmethod
    def _initial_library(library_root, cop):
        """Explicit edition, else the last one used, else the newest installed."""
        if cop:
            return Library.discover(library_root, cop=cop)
        remembered = _load_settings().get("cop")
        if remembered:
            try:
                return Library.discover(library_root, cop=remembered)
            except ValueError:
                pass
        return Library.discover(library_root)

    def _apply_theme(self):
        """Flat, high-contrast theme. Row height must follow the font."""
        p = PALETTE
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(family=UI_FONT, size=9)
            except Exception:
                pass
        try:
            line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        except Exception:
            line = 16
        bold = (UI_FONT_BOLD, 9)

        style.configure(".", background=p["bg"], foreground=p["text"],
                        bordercolor=p["border"], focuscolor=p["accent"],
                        troughcolor=p["heading"], selectbackground=p["select"],
                        selectforeground=p["text"])
        style.configure("TFrame", background=p["bg"])
        style.configure("Card.TFrame", background=p["surface"], relief="solid",
                        borderwidth=1, bordercolor=p["border"])
        style.configure("Status.TFrame", background=p["surface"])
        style.configure("Inner.TFrame", background=p["surface"])
        style.configure("TLabel", background=p["bg"], foreground=p["text"])
        style.configure("Muted.TLabel", background=p["bg"], foreground=p["muted"])
        style.configure("Card.TLabel", background=p["surface"], foreground=p["text"])
        style.configure("CardMuted.TLabel", background=p["surface"], foreground=p["muted"])
        style.configure("Status.TLabel", background=p["surface"], foreground=p["muted"],
                        padding=(10, 4))
        style.configure("Verdict.TLabel", background=p["bg"], font=(UI_FONT_BOLD, 11))

        style.configure("TButton", background=p["surface"], foreground=p["text"],
                        bordercolor="#c9d0db", lightcolor=p["surface"],
                        darkcolor=p["surface"], relief="raised", padding=(12, 5))
        style.map("TButton",
                  background=[("disabled", p["bg"]), ("pressed", p["heading"]),
                              ("active", p["hover"])],
                  foreground=[("disabled", "#a0a8b5")],
                  bordercolor=[("focus", p["accent"])])
        style.configure("Accent.TButton", background=p["accent"], foreground="#ffffff",
                        bordercolor=p["accent"], lightcolor=p["accent"],
                        darkcolor=p["accent"], font=bold, padding=(16, 5))
        style.map("Accent.TButton",
                  background=[("disabled", p["accent_disabled"]),
                              ("pressed", p["accent_pressed"]),
                              ("active", p["accent_hover"])],
                  foreground=[("disabled", "#eef5fd")],
                  bordercolor=[("disabled", p["accent_disabled"])])

        for name, bg in (("TCheckbutton", p["bg"]), ("Card.TCheckbutton", p["surface"])):
            style.configure(name, background=bg, foreground=p["text"],
                            indicatorbackground=p["surface"],
                            indicatorforeground="#ffffff", padding=(2, 2))
            style.map(name,
                      background=[("active", bg)],
                      indicatorbackground=[("selected", p["accent"]),
                                           ("active", p["hover"])])

        style.configure("TEntry", fieldbackground=p["surface"], bordercolor=p["border"],
                        lightcolor=p["surface"], darkcolor=p["surface"], padding=(5, 3))
        style.map("TEntry", bordercolor=[("focus", p["accent"])],
                  lightcolor=[("focus", p["accent"])])
        style.configure("TCombobox", fieldbackground=p["surface"], background=p["surface"],
                        bordercolor=p["border"], lightcolor=p["surface"],
                        darkcolor=p["surface"], arrowcolor=p["muted"], padding=(5, 3))
        style.map("TCombobox",
                  fieldbackground=[("readonly", p["surface"]), ("disabled", p["bg"])],
                  selectbackground=[("readonly", p["surface"])],
                  selectforeground=[("readonly", p["text"])],
                  bordercolor=[("focus", p["accent"])])
        self.option_add("*TCombobox*Listbox.font", (UI_FONT, 9))
        self.option_add("*TCombobox*Listbox.selectBackground", p["select"])
        self.option_add("*TCombobox*Listbox.selectForeground", p["text"])

        style.configure("TNotebook", background=p["bg"], borderwidth=0,
                        tabmargins=(0, 6, 0, 0))
        style.configure("TNotebook.Tab", background=p["bg"], foreground=p["muted"],
                        bordercolor=p["border"], lightcolor=p["bg"],
                        padding=(16, 7), font=bold)
        style.map("TNotebook.Tab",
                  background=[("selected", p["surface"]), ("active", p["hover"])],
                  foreground=[("selected", p["accent"])],
                  lightcolor=[("selected", p["surface"])],
                  expand=[("selected", (0, 0, 0, 0))])

        style.configure("Treeview", background=p["surface"], fieldbackground=p["surface"],
                        foreground=p["text"], bordercolor=p["border"],
                        lightcolor=p["surface"], darkcolor=p["surface"],
                        rowheight=max(24, line + 9))
        style.map("Treeview",
                  background=[("selected", p["select"])],
                  foreground=[("selected", p["text"])])
        style.configure("Treeview.Heading", background=p["heading"], foreground="#344054",
                        bordercolor=p["border"], lightcolor=p["heading"],
                        darkcolor=p["heading"], relief="flat", padding=(6, 5), font=bold)
        style.map("Treeview.Heading", background=[("active", p["hover"])])

        style.configure("Horizontal.TProgressbar", background=p["accent"],
                        troughcolor=p["heading"], bordercolor=p["surface"],
                        lightcolor=p["accent"], darkcolor=p["accent"], thickness=6)
        for orient in ("Vertical", "Horizontal"):
            style.configure("%s.TScrollbar" % orient, background="#cfd5df",
                            troughcolor=p["surface"], bordercolor=p["surface"],
                            lightcolor="#cfd5df", darkcolor="#cfd5df",
                            arrowcolor=p["muted"], gripcount=0)
            style.map("%s.TScrollbar" % orient,
                      background=[("active", "#b7bfcc"), ("pressed", "#a3adbc")])

    def _on_resize(self, event):
        if event.widget is not self:
            return
        try:
            self.warn_lbl.configure(wraplength=max(420, event.width - 60))
        except Exception:
            pass

    def _apply_initial_geometry(self):
        """Size and place the window after layout, restoring the last session."""
        try:
            self.update_idletasks()
            saved = _load_settings().get("geometry")
            if saved and _geometry_on_screen(saved, self):
                self.geometry(saved)
            else:
                width = min(1360, max(760, self.winfo_screenwidth() - 120))
                height = min(900, max(500, self.winfo_screenheight() - 120))
                x = max(0, (self.winfo_screenwidth() - width) // 2)
                y = max(0, (self.winfo_screenheight() - height) // 3)
                self.geometry("%dx%d+%d+%d" % (width, height, x, y))
            self.update_idletasks()
        except Exception:
            pass

    def close(self):
        if self.busy and self._worker is not None and self._worker.is_alive():
            self._closing = True
            self.cancel_work()
            self.set_status("Cancelling current work before closing ...")
            return
        self._close_now()

    def _close_now(self):
        try:
            if self.state() == "normal":
                _update_settings(geometry=self.geometry())
        except Exception:
            pass
        if self._poll_job is not None:
            try:
                self.after_cancel(self._poll_job)
            except Exception:
                pass
            self._poll_job = None
        self._cancel_filter_jobs()
        if getattr(self, "_preview_job", None) is not None:
            try:
                self.after_cancel(self._preview_job)
            except Exception:
                pass
            self._preview_job = None
        if self.locator is not None:
            self.locator.close()
            self.locator = None
        if getattr(builtins, SINGLETON_NAME, None) is self:
            delattr(builtins, SINGLETON_NAME)
        MstnTk.close(self)

    # -- layout -------------------------------------------------------------

    def _build(self):
        p = PALETTE
        header = tk.Frame(self, background=p["header"], padx=16, pady=10)
        header.pack(fill="x")
        brand = tk.Frame(header, background=p["header"])
        brand.pack(side="left")
        tk.Label(brand, text="IFC+SG Checker", background=p["header"],
                 foreground=p["header_text"], font=(UI_FONT_BOLD, 13)).pack(anchor="w")
        tk.Label(brand, text="CORENET X pre-flight for MicroStation  \u00b7  unofficial tool",
                 background=p["header"], foreground=p["header_muted"],
                 font=(UI_FONT, 8)).pack(anchor="w")

        cop = tk.Frame(header, background=p["header"])
        cop.pack(side="right")
        tk.Label(cop, text="CODE OF PRACTICE", background=p["header"],
                 foreground=p["header_muted"], font=(UI_FONT_BOLD, 7)).pack(anchor="w")
        cop_row = tk.Frame(cop, background=p["header"])
        cop_row.pack(anchor="w")
        self.cop_var = tk.StringVar()
        self.cop_box = ttk.Combobox(cop_row, textvariable=self.cop_var,
                                    state="readonly", width=38)
        self.cop_box.pack(side="left")
        self.cop_box.bind("<<ComboboxSelected>>", self.on_cop_change)
        self.cop_link = tk.Label(cop_row, text="COP document \u2197", cursor="hand2",
                                 background=p["header"], foreground="#8cc4ff",
                                 font=(UI_FONT, 8, "underline"), padx=10)
        self.cop_link.pack(side="left")
        self.cop_link.bind("<Button-1>", lambda _e: self.open_cop_document())

        body = ttk.Frame(self, padding=(12, 10, 12, 0))
        body.pack(fill="both", expand=True)

        card = ttk.Frame(body, style="Card.TFrame", padding=(12, 10))
        card.pack(fill="x")
        bar = ttk.Frame(card, style="Inner.TFrame")
        bar.pack(fill="x")
        ttk.Label(bar, text="Source", style="CardMuted.TLabel").pack(side="left", padx=(0, 8))
        self.source_var = tk.StringVar()
        self.source_box = ttk.Combobox(bar, textvariable=self.source_var,
                                       state="readonly", width=60)
        self.source_box.pack(side="left", fill="x", expand=True)
        self.cancel_btn = ttk.Button(bar, text="Cancel", command=self.cancel_work)
        self.cancel_btn.pack(side="right", padx=(6, 0))
        self.cancel_btn.state(["disabled"])
        self.run_btn = ttk.Button(bar, text="\u25b6  Run check", style="Accent.TButton",
                                  command=self.run_check)
        self.run_btn.pack(side="right", padx=(10, 0))
        self.browse_btn = ttk.Button(bar, text="Browse IFC...", command=self.browse)
        self.browse_btn.pack(side="right", padx=(6, 0))
        self.refresh_btn = ttk.Button(bar, text="Refresh", command=self.refresh_sources)
        self.refresh_btn.pack(side="right", padx=(6, 0))

        self.stamp_lbl = ttk.Label(card, text="", style="CardMuted.TLabel")
        self.stamp_lbl.pack(fill="x", pady=(8, 0))

        self.banner = ttk.Frame(body)
        self.warn_lbl = tk.Label(self.banner, background=p["banner"],
                                 foreground=p["banner_text"], anchor="w",
                                 justify="left", wraplength=900, padx=12, pady=7,
                                 font=(UI_FONT, 9))
        self.warn_lbl.pack(fill="x")

        summary = ttk.Frame(body)
        summary.pack(fill="x", pady=(10, 2))
        self.summary_frame = summary
        self._refresh_stamp()
        self.verdict_lbl = ttk.Label(summary, text="No check run yet. Choose a source "
                                                   "and click Run check.",
                                     style="Verdict.TLabel")
        self.verdict_lbl.pack(side="left", fill="x", expand=True)
        self.chips = {}
        for status in (PASS, UNKNOWN, WARN, FAIL):
            chip = tk.Label(summary, text="%s  \u2013" % STATUS_GLYPH[status],
                            background=STATUS_TINT[status],
                            foreground=STATUS_COLOUR[status],
                            font=(UI_FONT_BOLD, 9), padx=10, pady=3, cursor="hand2")
            chip.pack(side="right", padx=(6, 0))
            chip.bind("<Button-1>", lambda _e, s=status: self.toggle_status(s))
            self.chips[status] = chip

        split = ttk.PanedWindow(body, orient="horizontal")
        split.pack(fill="both", expand=True, pady=(6, 8))
        nb = ttk.Notebook(split)
        self.notebook = nb
        self.fix_tab = ttk.Frame(nb, padding=(10, 8), style="Inner.TFrame")
        self.rules_tab = ttk.Frame(nb, padding=(10, 8), style="Inner.TFrame")
        self.objects_tab = ttk.Frame(nb, padding=(10, 8), style="Inner.TFrame")
        self.breakdown_tab = ttk.Frame(nb, padding=(10, 8), style="Inner.TFrame")
        nb.add(self.fix_tab, text="Objects to fix")
        nb.add(self.rules_tab, text="Rules and findings")
        nb.add(self.objects_tab, text="Object checklist")
        nb.add(self.breakdown_tab, text="By discipline and storey")
        self._build_fix_tab()
        self._build_rules_tab()
        self._build_objects_tab()
        self._build_breakdown_tab()
        self.preview_panel = ttk.Frame(split, style="Card.TFrame", padding=(10, 8))
        split.add(nb, weight=3)
        split.add(self.preview_panel, weight=2)
        self._build_preview_panel()

        status_bar = ttk.Frame(self, style="Status.TFrame")
        status_bar.pack(fill="x", side="bottom", before=body)
        tk.Frame(self, background=p["border"], height=1).pack(
            fill="x", side="bottom", before=body)
        self.status = ttk.Label(status_bar, text="Ready.", style="Status.TLabel", anchor="w")
        self.status.pack(fill="x", side="left", expand=True)
        self.progress = ttk.Progressbar(status_bar, length=180, mode="determinate",
                                        maximum=100)
        self.progress.pack(side="right", padx=10)
        self._load_editions()

    # -- COP editions ---------------------------------------------------------

    def _load_editions(self):
        try:
            self.editions = Library.available(self.library.root)
        except Exception:
            self.editions = []
        self.cop_box["values"] = [edition.label for edition in self.editions]
        self._show_current_edition()

    def _show_current_edition(self):
        want = os.path.normcase(os.path.abspath(self.library.path))
        for index, edition in enumerate(self.editions):
            if os.path.normcase(os.path.abspath(edition.path)) == want:
                self.cop_box.current(index)
                return

    def on_cop_change(self, _event=None):
        index = self.cop_box.current()
        if index < 0 or index >= len(self.editions):
            return
        edition = self.editions[index]
        if os.path.normcase(os.path.abspath(edition.path)) == os.path.normcase(
                os.path.abspath(self.library.path)):
            return
        if self.busy:
            self._show_current_edition()
            self.set_status("Wait for the current check to finish before changing COP.")
            return
        try:
            library = Library(self.library.root, edition.path)
            library.latest_cop = self.editions[0].cop_edition
        except Exception as exc:
            self._show_current_edition()
            messagebox.showerror("IFC+SG Checker",
                                 "Cannot load COP %s:\n%s" % (edition.cop_edition, exc),
                                 parent=self)
            return
        if self.ifc is not None and self.source is not None:
            self._rerun_rules(library)
        else:
            self._commit_library(library)
            self.set_status("COP %s selected." % edition.cop_edition)

    def _commit_library(self, library):
        self.library = library
        self.engine = Engine(library)
        _update_settings(cop=library.editions["cop_edition"])
        self._refresh_stamp()
        self._show_current_edition()

    def _rerun_rules(self, library):
        """Re-evaluate the parsed model against another COP edition.

        The new edition is committed only when the re-check succeeds, so a
        cancelled or failed run never shows old results under a new COP header.
        """
        parsed, source = self.ifc, self.source
        engine = Engine(library)
        self.set_status("Re-checking against COP %s ..." % library.editions["cop_edition"])

        def work(cancel, progress):
            ctx, results = engine.run(
                parsed, progress=lambda label, done, total: progress(label, done, total),
                cancel=cancel)
            return source, parsed, ctx, report_mod.Report(
                source, parsed, ctx, results, library)

        def done(payload):
            self._commit_library(library)
            self._finish_check(payload)

        def failed(exc, details):
            self._show_current_edition()
            messagebox.showerror(
                "IFC+SG Checker", "Re-check failed:\n%s\n\n%s" % (exc, details), parent=self)
            self.set_status("Re-check failed; results still use COP %s."
                            % self.library.editions["cop_edition"])

        def cancelled():
            self._show_current_edition()
            self.set_status("Cancelled; results still use COP %s."
                            % self.library.editions["cop_edition"])

        self._start_worker(work, done, failed, cancel_callback=cancelled)

    def open_cop_document(self):
        meta = self.library.metadata
        url = meta.get("cop_url") or meta.get("official_cop_url")
        if url:
            webbrowser.open(url)

    def toggle_status(self, status):
        var = self.show.get(status)
        if var is not None:
            var.set(not var.get())
            self.populate()

    def _update_chips(self):
        counts = self.report.counts if self.report is not None else None
        for status, chip in self.chips.items():
            value = "\u2013" if counts is None else str(counts.get(status, 0))
            chip.configure(text="%s  %s" % (STATUS_GLYPH[status], value))

    def _refresh_stamp(self):
        ed = self.library.editions
        age = ed["mapping_age_days"]
        age_text = "" if age is None else " (%d days old)" % age
        self.stamp_lbl.configure(
            text="COP %s  \u00b7  IFC+SG mapping %s%s  \u00b7  %d property sets  \u00b7  "
                 "%d identified components"
                 % (ed["cop_edition"], ed["mapping_edition"], age_text,
                    ed["sgpset_count"], ed["identified_components"]))
        if not self.library.is_complete:
            text = ("The selected catalogue is marked incomplete. Rebuild it from the current "
                    "official BCA mapping before relying on property checks.")
        elif ed.get("superseded"):
            text = ("COP %s is superseded by COP %s. Use it only for projects still "
                    "assessed under COP %s." % (ed["cop_edition"], ed["latest_cop"],
                                                 ed["cop_edition"]))
        elif age is not None and age > 365:
            text = ("Mapping edition %s is %d days old. The BCA workbook is versioned and "
                    "work-in-progress: verify the edition against info.corenet.gov.sg "
                    "before relying on this for a submission." % (ed["mapping_edition"], age))
        else:
            text = ""
        self.warn_lbl.configure(text=text)
        # Show or hide the whole banner frame: an emptied Tk frame keeps its
        # last height, which left a blank gap after a superseded COP.
        if text:
            self.banner.pack(fill="x", pady=(8, 0), before=self.summary_frame)
        else:
            self.banner.pack_forget()

    # -- objects to fix and preview ------------------------------------------

    def _build_fix_tab(self):
        bar = ttk.Frame(self.fix_tab, style="Inner.TFrame")
        bar.pack(fill="x", pady=(2, 6))
        ttk.Label(bar, text="Find", style="CardMuted.TLabel").pack(side="left", padx=(0, 6))
        self.fix_filter = tk.StringVar()
        self.fix_filter.trace_add(
            "write", lambda *_a: self._schedule_filter("fix", self.populate_fix))
        ttk.Entry(bar, textvariable=self.fix_filter, width=34).pack(side="left")
        self.fix_errors_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Errors only", style="Card.TCheckbutton",
                        variable=self.fix_errors_only,
                        command=self.populate_fix).pack(side="left", padx=12)
        self.fix_count = ttk.Label(bar, text="", style="CardMuted.TLabel")
        self.fix_count.pack(side="right")

        wrap = ttk.Frame(self.fix_tab, style="Inner.TFrame")
        wrap.pack(fill="both", expand=True)
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        cols = ("sev", "entity", "storey", "issues", "reason")
        self.fix_tree = ttk.Treeview(wrap, columns=cols, show="tree headings",
                                     selectmode="browse")
        self.fix_tree.heading("#0", text="Object")
        for col, title, width, stretch in (
                ("sev", "Severity", 80, False), ("entity", "IFC class", 150, False),
                ("storey", "Storey", 120, False), ("issues", "Issues", 60, False),
                ("reason", "Main reason", 360, True)):
            self.fix_tree.heading(col, text=title)
            self.fix_tree.column(col, width=width, minwidth=50, stretch=stretch,
                                 anchor="center" if col == "issues" else "w")
        self.fix_tree.column("#0", width=200, minwidth=120, stretch=False)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.fix_tree.yview)
        self.fix_tree.configure(yscroll=vsb.set)
        self.fix_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self.fix_tree.tag_configure("ERROR", foreground=STATUS_COLOUR[FAIL])
        self.fix_tree.tag_configure("WARN", foreground=STATUS_COLOUR[WARN])
        self.fix_tree.bind("<<TreeviewSelect>>", self.on_fix_select)
        self.fix_tree.bind("<Double-1>", lambda _e: self.preview_select())
        ttk.Label(self.fix_tab, style="CardMuted.TLabel",
                  text="Click an object to see it and every reason it fails. Use the "
                       "arrow keys to step through. Double-click selects it in "
                       "MicroStation.").pack(anchor="w", pady=(6, 0))

    def _build_preview_panel(self):
        p = self.preview_panel
        self.preview_title = ttk.Label(p, text="No object selected", style="Card.TLabel",
                                       font=(UI_FONT_BOLD, 11))
        self.preview_title.pack(anchor="w")
        self.preview_meta = ttk.Label(p, text="Run a check, then pick an object.",
                                      style="CardMuted.TLabel", wraplength=360,
                                      justify="left")
        self.preview_meta.pack(anchor="w", fill="x", pady=(2, 6))
        self.preview_canvas = tk.Canvas(p, width=360, height=250, background="#ffffff",
                                        highlightthickness=1,
                                        highlightbackground=PALETTE["border"])
        self.preview_canvas.pack(fill="both", expand=False)
        self.preview_canvas.bind("<Configure>", lambda _e: self._redraw_preview())
        buttons = ttk.Frame(p, style="Inner.TFrame")
        buttons.pack(fill="x", pady=6)
        ttk.Button(buttons, text="Select in MicroStation",
                   command=self.preview_select).pack(side="left")
        ttk.Button(buttons, text="Isolate",
                   command=self.preview_isolate).pack(side="left", padx=4)
        ttk.Button(buttons, text="Copy GlobalId",
                   command=self.preview_copy_guid).pack(side="left")
        text_wrap = ttk.Frame(p, style="Inner.TFrame")
        text_wrap.pack(fill="both", expand=True)
        self.preview_text = tk.Text(text_wrap, wrap="word", height=12, relief="flat",
                                    background=PALETTE["surface"], foreground=PALETTE["text"],
                                    font=(UI_FONT, 9), padx=2, pady=2, cursor="arrow")
        sb = ttk.Scrollbar(text_wrap, orient="vertical", command=self.preview_text.yview)
        self.preview_text.configure(yscrollcommand=sb.set)
        self.preview_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.preview_text.tag_configure("ERROR", foreground=STATUS_COLOUR[FAIL],
                                        font=(UI_FONT_BOLD, 9))
        self.preview_text.tag_configure("WARN", foreground=STATUS_COLOUR[WARN],
                                        font=(UI_FONT_BOLD, 9))
        self.preview_text.tag_configure("muted", foreground=PALETTE["muted"])
        self.preview_text.tag_configure("fix", foreground="#344054",
                                        font=(UI_FONT, 9, "italic"))
        self.preview_text.configure(state="disabled")
        self.preview_id = None
        self._preview_wire = None
        self._preview_note = ""
        self._wire_cache = {}
        self._preview_job = None
        self._pictures = None

    def populate_fix(self):
        self.fix_tree.delete(*self.fix_tree.get_children())
        if self.report is None:
            self.fix_count.configure(text="")
            return
        want = self.fix_filter.get().strip().lower()
        errors_only = self.fix_errors_only.get()
        rows = self.report.failing_objects()
        shown = 0
        for row in rows:
            if errors_only and row["severity"] != "ERROR":
                continue
            first = row["issues"][0] if row["issues"] else {}
            reason = "%s  %s" % (first.get("rule", ""), first.get("message", ""))
            if want and want not in " ".join((
                    str(row["id"]), row["name"], row["entity"], row["storey"], row["guid"],
                    " ".join(i["message"] + " " + i["rule"] for i in row["issues"])
            )).lower():
                continue
            if shown >= MAX_VISIBLE_FINDINGS:
                break
            self.fix_tree.insert(
                "", "end", iid=str(row["id"]),
                text="%s  #%s" % (row["name"] or "(unnamed)", row["id"]),
                values=(row["severity"], row["entity"], row["storey"],
                        len(row["issues"]), reason),
                tags=(row["severity"],))
            shown += 1
        errors = sum(1 for r in rows if r["severity"] == "ERROR")
        self.fix_count.configure(
            text="%d shown  \u00b7  %d objects to fix (%d with errors)"
                 % (shown, len(rows), errors))

    def on_fix_select(self, _event=None):
        sel = self.fix_tree.selection()
        if sel:
            self.show_object(int(sel[0]))

    def show_object(self, oid):
        """Show one object and every reason it fails, in the preview panel."""
        if self.report is None or oid is None:
            return
        self.preview_id = oid
        info = self.report.describe_object(oid)
        issues = self.report.issue_index().get(oid, [])
        self.preview_title.configure(text="%s  #%s" % (info["name"] or "(unnamed)", oid))
        self.preview_meta.configure(
            text="%s  \u00b7  storey %s\nGlobalId %s"
                 % (info["entity"], info["storey"], info["guid"] or "-"))
        t = self.preview_text
        t.configure(state="normal")
        t.delete("1.0", "end")
        if not issues:
            t.insert("end", "No failing or warning rule names this object.", "muted")
        for issue in issues:
            t.insert("end", "%s  %s  %s\n" % (issue["severity"], issue["rule"],
                                              issue["title"]), issue["severity"])
            t.insert("end", issue["message"] + "\n")
            if issue["detail"]:
                t.insert("end", issue["detail"] + "\n", "muted")
            if issue["fix"]:
                t.insert("end", "Fix: %s\n" % issue["fix"], "fix")
            t.insert("end", "\n")
        t.configure(state="disabled")
        t.yview_moveto(0)
        # Debounced so holding an arrow key does not draw every row on the way.
        if self._preview_job is not None:
            try:
                self.after_cancel(self._preview_job)
            except Exception:
                pass
        self._preview_job = self.after(60, lambda: self._load_preview(oid))

    def _load_preview(self, oid):
        self._preview_job = None
        if oid != self.preview_id or self.report is None:
            return
        cached = self._wire_cache.get(oid)
        if cached is None:
            loc = self.locator
            indexing = (loc is not None and loc.available
                        and getattr(loc, "_guids", None) is None)
            if indexing:
                previous = self.status.cget("text")
                self.set_status("Indexing MicroStation elements for pictures (once per "
                                "check) ...")
            try:
                cached = preview_mod.object_wire(self.report, oid, self.locator)
            except Exception as exc:
                cached = ([], "Could not draw the object: %s" % exc)
            if indexing:
                self.set_status(previous)
            if len(self._wire_cache) > 300:
                self._wire_cache.clear()
            self._wire_cache[oid] = cached
        self._preview_wire, self._preview_note = cached
        self._redraw_preview()

    def _redraw_preview(self):
        c = self.preview_canvas
        w = max(c.winfo_width(), 100)
        h = max(c.winfo_height(), 80)
        if self.preview_id is None:
            preview_mod.draw(c, [], w, h, "Pick an object to see it here.")
            return
        preview_mod.draw(c, self._preview_wire or [], w, h,
                         self._preview_note or "No picture available.")
        if self._preview_wire and self._preview_note:
            c.create_text(8, 8, anchor="nw", text=self._preview_note, width=w - 16,
                          fill=PALETTE["muted"], font=(UI_FONT, 8))

    def _clear_preview(self):
        if self._preview_job is not None:
            try:
                self.after_cancel(self._preview_job)
            except Exception:
                pass
            self._preview_job = None
        self.preview_id = None
        self._preview_wire = None
        self._preview_note = ""
        self._wire_cache = {}
        self._pictures = None
        self.preview_title.configure(text="No object selected")
        self.preview_meta.configure(text="Run a check, then pick an object.")
        self.preview_text.configure(state="normal")
        self.preview_text.delete("1.0", "end")
        self.preview_text.configure(state="disabled")
        self._redraw_preview()

    def preview_select(self):
        if self.preview_id is not None:
            self._do_select([self.preview_id])

    def preview_isolate(self):
        if self.preview_id is None or not self._locator_ready():
            return
        _count, note = self.locator.isolate([self.preview_id])
        self.set_status(note)

    def preview_copy_guid(self):
        if self.preview_id is None or self.report is None:
            self.set_status("Pick an object first.")
            return
        guid = self.report.describe_object(self.preview_id)["guid"]
        if not guid:
            self.set_status("This object has no GlobalId.")
            return
        self.clipboard_clear()
        self.clipboard_append(guid)
        self.set_status("Copied GlobalId %s" % guid)

    def report_pictures(self):
        """Pictures for the HTML report, drawn once per check on this thread."""
        if self._pictures is not None or self.report is None:
            return self._pictures or {}

        def progress(label, done, total):
            self.status.configure(text=label)
            self.progress["value"] = int(done * 100.0 / max(total, 1))
            try:
                self.update_idletasks()
            except Exception:
                pass

        try:
            self._pictures = preview_mod.collect_pictures(
                self.report, self.locator, progress=progress)
        finally:
            self.progress["value"] = 0
        self.set_status("Drew %d object picture(s) for the report."
                        % len(self._pictures or {}))
        return self._pictures

    def _build_rules_tab(self):
        status_row = ttk.Frame(self.rules_tab, style="Inner.TFrame")
        status_row.pack(fill="x", pady=(4, 0))
        ttk.Label(status_row, text="Show", style="CardMuted.TLabel").pack(side="left")
        self.show = {}
        for status in (FAIL, WARN, UNKNOWN, PASS, NOT_APPLICABLE):
            var = tk.BooleanVar(value=status in (FAIL, WARN, UNKNOWN))
            self.show[status] = var
            ttk.Checkbutton(status_row, style="Card.TCheckbutton", text=STATUS_GLYPH[status], variable=var,
                            command=self.populate).pack(side="left", padx=3)
        # Explicit selection avoids an accidental first-click triggering a native
        # full-model identity/range index on a very large reference.
        self.autoselect = tk.BooleanVar(value=False)
        ttk.Checkbutton(status_row, style="Card.TCheckbutton", text="Select in MicroStation on click",
                        variable=self.autoselect).pack(side="left", padx=14)
        ttk.Checkbutton(status_row, style="Card.TCheckbutton", text="Keep on top", variable=self.keep_on_top,
                        command=self._apply_on_top).pack(side="left")
        ttk.Button(status_row, text="Clear filters",
                   command=self.clear_filters).pack(side="right")

        # A single inline bar. Boxes are not stacked under the headings: entry
        # widths are in characters and tree columns in pixels, so they cannot
        # track each other and the near-miss reads as a broken second header.
        filt = ttk.Frame(self.rules_tab, style="Inner.TFrame")
        filt.pack(fill="x", pady=(4, 2))
        ttk.Label(filt, text="Filter", style="CardMuted.TLabel").pack(side="left", padx=(0, 8))
        self.col_filters = {}
        for key, label, width in (("rule", "Rule", 12), ("status", "Status", 8),
                                  ("entity", "IFC entity", 12), ("name", "Name", 12),
                                  ("message", "Detail", 22)):
            ttk.Label(filt, text=label, style="CardMuted.TLabel").pack(side="left")
            var = tk.StringVar()
            var.trace_add(
                "write",
                lambda *_a: self._schedule_filter("rules", self.populate))
            self.col_filters[key] = var
            ttk.Entry(filt, textvariable=var, width=width).pack(side="left", padx=(3, 12))

        wrap = ttk.Frame(self.rules_tab, style="Inner.TFrame")
        wrap.pack(fill="both", expand=True)
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        cols = ("status", "entity", "name", "message")
        self.tree = ttk.Treeview(wrap, columns=cols, show="tree headings",
                                 selectmode="extended")
        self.tree.heading("#0", text="Rule")
        self.tree.heading("status", text="Status")
        self.tree.heading("entity", text="IFC entity")
        self.tree.heading("name", text="Name")
        self.tree.heading("message", text="Detail")
        self.tree.column("#0", width=290, minwidth=130, stretch=False)
        self.tree.column("status", width=90, minwidth=60, stretch=False, anchor="w")
        self.tree.column("entity", width=150, minwidth=90, stretch=False)
        self.tree.column("name", width=150, minwidth=80, stretch=False)
        self.tree.column("message", width=520, minwidth=220, stretch=True)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscroll=vsb.set, xscroll=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        for status, colour in STATUS_COLOUR.items():
            self.tree.tag_configure(status, foreground=colour)
        self.tree.tag_configure("rulerow", font=(UI_FONT_BOLD, 9), background="#f7f9fc")
        self.tree.tag_configure("finding", foreground="#344054")
        self.tree.bind("<<TreeviewSelect>>", self.on_row_select)
        self.tree.bind("<Double-1>", self.on_row_activate)

        act = ttk.Frame(self.rules_tab, style="Inner.TFrame")
        act.pack(fill="x", pady=5)
        ttk.Button(act, text="Select in MicroStation",
                   command=self.select_current).pack(side="left")
        ttk.Button(act, text="Isolate", command=self.isolate_current).pack(side="left", padx=4)
        ttk.Button(act, text="Select all in rule",
                   command=self.select_rule).pack(side="left", padx=4)
        ttk.Button(act, text="Show all", command=self.show_all).pack(side="left")
        ttk.Button(act, text="Clear selection",
                   command=self.clear_selection).pack(side="left", padx=4)
        ttk.Button(act, text="Copy GlobalId",
                   command=self.copy_globalid).pack(side="left")
        ttk.Button(act, text="Open HTML report",
                   command=self.open_html).pack(side="right")
        ttk.Button(act, text="Export HTML",
                   command=lambda: self.export("html")).pack(side="right", padx=4)
        ttk.Button(act, text="Export BCF",
                   command=self.export_bcf).pack(side="right")
        self.shown_lbl = ttk.Label(act, text="", style="CardMuted.TLabel")
        self.shown_lbl.pack(side="right", padx=12)

    def _apply_on_top(self):
        try:
            self.attributes("-topmost", bool(self.keep_on_top.get()))
        except Exception:
            pass

    def clear_filters(self):
        self._filter_suspended = True
        self._cancel_filter_jobs()
        try:
            for var in self.col_filters.values():
                var.set("")
            for status, var in self.show.items():
                var.set(status in (FAIL, WARN, UNKNOWN))
        finally:
            self._filter_suspended = False
        self._cancel_filter_jobs()
        self.populate()

    def _schedule_filter(self, name, callback):
        """Debounce large tree rebuilds while the user is still typing."""
        if getattr(self, "_filter_suspended", False):
            return
        old = self._filter_jobs.pop(name, None)
        if old is not None:
            try:
                self.after_cancel(old)
            except Exception:
                pass

        def run():
            self._filter_jobs.pop(name, None)
            if not self._closing:
                callback()

        self._filter_jobs[name] = self.after(FILTER_DELAY_MS, run)

    def _cancel_filter_jobs(self):
        for job in list(self._filter_jobs.values()):
            try:
                self.after_cancel(job)
            except Exception:
                pass
        self._filter_jobs.clear()

    def _build_objects_tab(self):
        filt = ttk.Frame(self.objects_tab, style="Inner.TFrame")
        filt.pack(fill="x", pady=4)
        ttk.Label(filt, text="Filter", style="CardMuted.TLabel").pack(side="left", padx=(0, 8))
        self.obj_filters = {}
        for key, label, width in (("entity", "IFC class", 18), ("rules", "Failing rules", 26)):
            ttk.Label(filt, text=label, style="CardMuted.TLabel").pack(side="left")
            var = tk.StringVar()
            var.trace_add(
                "write",
                lambda *_a: self._schedule_filter("objects", self.populate_objects))
            self.obj_filters[key] = var
            ttk.Entry(filt, textvariable=var, width=width).pack(side="left", padx=(3, 12))
        self.only_failing = tk.BooleanVar(value=False)
        ttk.Checkbutton(filt, style="Card.TCheckbutton", text="Only classes with failures", variable=self.only_failing,
                        command=self.populate_objects).pack(side="left")

        wrap = ttk.Frame(self.objects_tab, style="Inner.TFrame")
        wrap.pack(fill="both", expand=True, pady=4)
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        cols = ("total", "conforming", "failing", "rules")
        self.obj_tree = ttk.Treeview(wrap, columns=cols, show="tree headings")
        self.obj_tree.heading("#0", text="IFC class")
        for col, width, title in (("total", 80, "Objects"), ("conforming", 100, "Conforming"),
                                  ("failing", 90, "Failing"), ("rules", 520, "Failing rules")):
            self.obj_tree.heading(col, text=title)
            self.obj_tree.column(col, width=width, minwidth=60,
                                 stretch=(col == "rules"),
                                 anchor="center" if col != "rules" else "w")
        self.obj_tree.column("#0", width=220, minwidth=130, stretch=False)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.obj_tree.yview)
        hsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.obj_tree.xview)
        self.obj_tree.configure(yscroll=vsb.set, xscroll=hsb.set)
        self.obj_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        self.obj_tree.bind("<Double-1>", self.on_class_activate)
        ttk.Label(self.objects_tab,
                  text="Double-click a class to select every failing object of that class.",
                  style="CardMuted.TLabel").pack(anchor="w")

    def _build_breakdown_tab(self):
        self.bd_nodes = {}

        bar = ttk.Frame(self.breakdown_tab, style="Inner.TFrame")
        bar.pack(fill="x", pady=4)
        ttk.Label(bar, text="Filter", style="CardMuted.TLabel").pack(side="left", padx=(0, 8))
        self.bd_filters = {}
        for key, label, width in (("discipline", "Discipline", 16),
                                  ("storey", "Storey", 14),
                                  ("entity", "IFC class", 14)):
            ttk.Label(bar, text=label, style="CardMuted.TLabel").pack(side="left")
            var = tk.StringVar()
            var.trace_add(
                "write",
                lambda *_a: self._schedule_filter("breakdown", self.populate_breakdown))
            self.bd_filters[key] = var
            ttk.Entry(bar, textvariable=var, width=width).pack(side="left", padx=(3, 12))
        self.bd_only_failing = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, style="Card.TCheckbutton", text="Only rows with failures", variable=self.bd_only_failing,
                        command=self.populate_breakdown).pack(side="left")

        wrap = ttk.Frame(self.breakdown_tab, style="Inner.TFrame")
        wrap.pack(fill="both", expand=True)
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        cols = ("total", "failing", "conforming", "rules")
        self.bd_tree = ttk.Treeview(wrap, columns=cols, show="tree headings")
        self.bd_tree.heading("#0", text="Discipline / storey / IFC class")
        for col, title, width in (("total", "Total", 70), ("failing", "Failing", 70),
                                  ("conforming", "Conforming", 90), ("rules", "Rules", 380)):
            self.bd_tree.heading(col, text=title)
            self.bd_tree.column(col, width=width, minwidth=60,
                                stretch=(col == "rules"),
                                anchor="center" if col != "rules" else "w")
        self.bd_tree.column("#0", width=300, minwidth=160, stretch=False)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.bd_tree.yview)
        hsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.bd_tree.xview)
        self.bd_tree.configure(yscroll=vsb.set, xscroll=hsb.set)
        self.bd_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        for status, colour in STATUS_COLOUR.items():
            self.bd_tree.tag_configure(status, foreground=colour)
        self.bd_tree.bind("<Double-1>", self.on_breakdown_activate)
        ttk.Label(self.breakdown_tab,
                  text="Double-click any row to select those elements in MicroStation.",
                  style="CardMuted.TLabel").pack(anchor="w")

    # -- background work ----------------------------------------------------

    def _set_busy(self, busy, cancellable=True):
        self.busy = busy
        controls = (self.run_btn, self.refresh_btn, self.browse_btn)
        for control in controls:
            control.state(["disabled"] if busy else ["!disabled"])
        self.source_box.configure(state="disabled" if busy else "readonly")
        self.cop_box.configure(state="disabled" if busy else "readonly")
        self.cancel_btn.state(
            ["!disabled"] if busy and cancellable else ["disabled"])
        if not busy:
            self.progress["value"] = 0

    def _start_worker(self, action, callback, error_callback=None, cancellable=True,
                      cancel_callback=None):
        """Run pure Python/file work without blocking MicroStation or Tk."""
        if self.busy:
            return
        self._worker_queue = queue.Queue()
        self._worker_callback = callback
        self._worker_error_callback = error_callback
        self._worker_cancel_callback = cancel_callback
        self._cancel_event = threading.Event()
        self._set_busy(True, cancellable=cancellable)

        def post_progress(text, completed=None, total=None):
            self._worker_queue.put(("progress", text, completed, total))

        def run():
            try:
                result = action(self._cancel_event.is_set, post_progress)
                if self._cancel_event.is_set():
                   self._worker_queue.put(("cancelled",))
                else:
                   self._worker_queue.put(("result", result))
            except Exception as exc:
                if isinstance(exc, spf.OperationCancelled):
                   self._worker_queue.put(("cancelled",))
                else:
                   self._worker_queue.put(
                       ("error", exc, traceback.format_exc()))

        # A daemon cannot keep MicroStation's embedded Python alive during
        # shutdown. Cancellation checkpoints normally stop it much earlier.
        self._worker = threading.Thread(
            target=run, name="IFCSG-checker-worker", daemon=True)
        self._worker.start()
        self._poll_worker()

    def _poll_worker(self):
        terminal = None
        while True:
            try:
                message = self._worker_queue.get_nowait()
            except queue.Empty:
                break
            if message[0] == "progress":
                _kind, text, completed, total = message
                self.status.configure(text=str(text))
                if total:
                   self.progress["value"] = max(
                       0, min(100, int(float(completed or 0) * 100.0 / total)))
                else:
                   self.progress["value"] = 0
            else:
                terminal = message

        worker_alive = self._worker is not None and self._worker.is_alive()
        if terminal is None and not worker_alive:
            # Final drain after observing thread exit closes the small race where
            # the result was queued between the first empty read and is_alive().
            while True:
                try:
                    message = self._worker_queue.get_nowait()
                except queue.Empty:
                    break
                if message[0] == "progress":
                    _kind, text, completed, total = message
                    self.status.configure(text=str(text))
                    if total:
                        self.progress["value"] = max(
                            0, min(100, int(float(completed or 0) * 100.0 / total)))
                else:
                    terminal = message

        if terminal is None and worker_alive:
            self._poll_job = self.after(75, self._poll_worker)
            return
        if terminal is None:
            terminal = (
                "error", RuntimeError("Background worker stopped without a result."), "")

        self._poll_job = None
        self._set_busy(False)
        callback = self._worker_callback
        error_callback = self._worker_error_callback
        cancel_callback = getattr(self, "_worker_cancel_callback", None)
        self._worker = None
        self._worker_callback = None
        self._worker_error_callback = None
        self._worker_cancel_callback = None
        self._cancel_event = None

        if self._closing:
            self._close_now()
            return
        if terminal is None or terminal[0] == "cancelled":
            self.set_status("Cancelled.")
            if cancel_callback is not None:
                cancel_callback()
            return
        if terminal[0] == "error":
            _kind, exc, details = terminal
            if error_callback is not None:
                error_callback(exc, details)
            else:
                messagebox.showerror(
                   "IFC+SG Checker", "Operation failed:\n%s\n\n%s" % (exc, details),
                   parent=self)
                self.set_status("Failed: %s" % exc)
            return
        callback(terminal[1])

    def cancel_work(self):
        if self._cancel_event is not None:
            self._cancel_event.set()
            self.cancel_btn.state(["disabled"])
            self.set_status("Cancelling ...")

    # -- sources ------------------------------------------------------------

    def refresh_sources(self):
        if self.busy:
            return
        self.sources = src_mod.discover()
        labels = [s.label for s in self.sources]
        self.source_box["values"] = labels
        if labels:
            preferred = 0
            for i, source in enumerate(self.sources):
                if source.is_ifc:
                    preferred = i
                    break
            self.source_box.current(preferred)
            self.set_status("%d source(s) found." % len(self.sources))
        else:
            detail = src_mod.LAST_DISCOVERY_ERROR or src_mod.IMPORT_ERROR
            if detail:
                self.set_status("No sources found: %s" % detail)
            else:
                self.set_status("No sources found. Use Browse IFC... to pick a file.")

    def browse(self):
        path = filedialog.askopenfilename(
            title="Select an IFC file", parent=self,
            filetypes=[("IFC files", "*.ifc *.ifczip"),
                       ("IFC-SPF", "*.ifc"), ("IFCZIP", "*.ifczip"),
                       ("All files", "*.*")])
        if not path:
            return
        self.sources.append(src_mod.browsed_source(path))
        self.source_box["values"] = [s.label for s in self.sources]
        self.source_box.current(len(self.sources) - 1)
        self.set_status("Selected %s" % os.path.basename(path))

    def current_source(self):
        idx = self.source_box.current()
        if idx < 0 or idx >= len(self.sources):
            return None
        return self.sources[idx]

    # -- run ----------------------------------------------------------------

    def run_check(self):
        if self.busy:
            return
        source = self.current_source()
        if source is None:
            messagebox.showinfo("IFC+SG Checker", "Choose a source first.", parent=self)
            return
        if not source.is_ifc:
            self._set_busy(True, cancellable=False)
            try:
                self.run_dgn_report(source)
            finally:
                self._set_busy(False)
            return
        if source.attachment is not None:
            source.path = src_mod.resolve_source_path(source)
        if not source.path or not os.path.isfile(source.path):
            missing = source.path or "(no path)"
            self.set_status("Reference source not found: %s" % missing)
            if messagebox.askyesno(
                    "IFC reference source not found",
                    "MicroStation reported this reference as:\n\n%s\n\n"
                    "The original IFC file is not available at that resolved path. "
                    "Browse to the IFC file now?"
                    % missing,
                    parent=self, icon="warning"):
                path = filedialog.askopenfilename(
                    title="Locate referenced IFC", parent=self,
                    initialfile=os.path.basename(missing),
                    filetypes=[("IFC files", "*.ifc *.ifczip"),
                               ("All files", "*.*")])
                if path:
                    source.path = path
                    source.label = "Reference: %s" % os.path.basename(path)
                    self.source_box["values"] = [s.label for s in self.sources]
            if not source.path or not os.path.isfile(source.path):
                return
        if not self._memory_preflight(source.path):
            return
        self._discard_report()
        self.set_status("Parsing %s ..." % os.path.basename(source.path))
        engine = self.engine
        library = self.library

        def work(cancel, progress):
            def parse_progress(done, total):
                progress("Parsing %s ..." % os.path.basename(source.path), done, total)

            try:
                retain = spf.source_text_size(source.path) <= spf.GEOMETRY_RETAIN_MAX_BYTES
            except Exception:
                retain = False
            parsed = IfcFile.read(source.path, progress=parse_progress, cancel=cancel,
                                  retain_geometry=retain)
            progress("Parsed %d entities. Building model index ..." % parsed.count(), 0, 4)

            def rule_progress(label, done, total):
                progress(label, done, total)

            ctx, results = engine.run(
                parsed, progress=rule_progress, cancel=cancel)
            report = report_mod.Report(source, parsed, ctx, results, library)
            return source, parsed, ctx, report

        self._start_worker(work, self._finish_check)

    def _discard_report(self):
        """Release the prior model before allocating the next parse."""
        if self.locator is not None:
            self.locator.close()
        self.locator = None
        self.report = None
        self.ifc = None
        self.source = None
        self.last_html = None
        self.row_data.clear()
        self.bd_nodes.clear()
        self.tree.delete(*self.tree.get_children())
        self.obj_tree.delete(*self.obj_tree.get_children())
        self.bd_tree.delete(*self.bd_tree.get_children())
        self.fix_tree.delete(*self.fix_tree.get_children())
        self._clear_preview()
        self._update_chips()
        gc.collect()

    def _memory_preflight(self, path):
        """Warn before a parse that is likely to exhaust the host process."""
        try:
            source_size = spf.source_text_size(path)
        except Exception:
            return True
        available = _available_physical_memory()
        if available is None:
            return True
        # Measured production models peak around 4.3x SPF size. Keep margin for
        # MicroStation, its references and transient report construction.
        required = max(512 * 1024 * 1024, int(source_size * 5.5))
        if available >= required:
            return True
        return messagebox.askyesno(
            "Large IFC memory warning",
            "This IFC expands to about %.0f MB of text. The checker estimates %.0f MB "
            "of free memory is needed, but Windows currently reports %.0f MB.\n\n"
            "Continuing may make MicroStation unstable. Continue anyway?"
            % (source_size / 1048576.0, required / 1048576.0,
               available / 1048576.0),
            parent=self, icon="warning")

    def _finish_check(self, payload):
        source, parsed, ctx, report = payload
        reuse = (self.locator is not None and self.source is source
                 and self.ifc is parsed)
        if self.locator is not None and not reuse:
            self.locator.close()
        self.ifc = parsed
        self.source = source
        self.report = report
        # Locator construction touches MSPy and therefore stays on this main thread.
        # A COP re-check keeps the same file, so its GlobalId index is reused.
        if not reuse:
            self.locator = Locator(source, parsed, ctx.geo)
        self.last_html = None
        self._wire_cache = {}
        self._pictures = None
        self.populate()
        self.populate_objects()
        self.populate_breakdown()
        self.populate_fix()
        self._update_chips()
        first = self.fix_tree.get_children()
        if first:
            self.notebook.select(self.fix_tab)
            self.fix_tree.selection_set(first[0])
            self.fix_tree.focus(first[0])
            self.fix_tree.focus_set()
        else:
            self._clear_preview()
        self.verdict_lbl.configure(
            text=self.report.verdict,
            foreground=STATUS_COLOUR[FAIL] if self.report.counts[FAIL]
            else STATUS_COLOUR[WARN] if self.report.counts[WARN]
            else STATUS_COLOUR[PASS])
        note = ("" if self.locator.available
                else "  Locate unavailable: %s" % self.locator.reason)
        if self.locator.available:
            note = "  Exact GlobalId selection is available when exposed by MicroStation."
        self.set_status("Done. %d elements, %d rule failures, %d element errors.%s"
                        % (len(ctx.element_ids), self.report.counts[FAIL],
                           self.report.error_findings, note))

    def run_dgn_report(self, source):
        self.set_status("Inspecting DGN model ...")
        info = src_mod.inspect_dgn(source)
        self.tree.delete(*self.tree.get_children())
        self.row_data.clear()
        self.shown_lbl.configure(text="")
        root = self.tree.insert(
            "", "end", text="DGN model inspection",
            values=(STATUS_GLYPH[UNKNOWN], "", "", source.label),
            tags=(UNKNOWN, "rulerow"))
        for label, value in (("Graphic elements", info["elements"]),
                             ("With Item Type data", info["with_items"]),
                             ("With business EC data", info["with_ec"])):
            self.tree.insert(root, "end", text="    " + label,
                             values=("", "", "", str(value)))
        for key, count in sorted(info["ec_classes"].items()):
            self.tree.insert(root, "end", text="    " + key,
                             values=("", "EC class", "", "%d element(s)" % count))
        for key, count in sorted(info["item_classes"].items()):
            self.tree.insert(root, "end", text="    " + key,
                             values=("", "Item Type", "", "%d element(s)" % count))
        for err in info["errors"]:
            self.tree.insert(root, "end", text="    error", values=("", "", "", err))
        if info.get("truncated"):
            self.tree.insert(
                root, "end", text="    safety limit",
                values=(STATUS_GLYPH[WARN], "", "",
                        "Inspection stopped after %d elements to keep MicroStation responsive."
                        % info["elements"]),
                tags=(WARN,))
        self.tree.item(root, open=True)
        verdict = ("No IFC+SG data on this DGN model. IFC+SG rules cannot be evaluated "
                   "against geometry alone."
                   if info["with_ec"] == 0 and info["with_items"] == 0 else
                   "DGN carries EC/Item Type data. Map it to IFC entities in OpenBuildings "
                   "Designer, export IFC4 Reference View, then check the IFC.")
        self.verdict_lbl.configure(text=verdict, foreground=STATUS_COLOUR[UNKNOWN])
        suffix = " (safety limit reached)" if info.get("truncated") else ""
        self.set_status("DGN inspection complete: %d graphic elements%s."
                        % (info["elements"], suffix))

    # -- population ---------------------------------------------------------

    def populate(self):
        if self.report is None:
            return
        want = dict((k, v.get().strip().lower()) for k, v in self.col_filters.items())
        row_filters_active = any(want[k] for k in ("entity", "name", "message"))
        self.tree.delete(*self.tree.get_children())
        self.row_data.clear()
        shown = 0
        visible_findings = 0

        for res in self.report.results:
            if not self.show[res.status].get():
                continue
            rule_hay = " ".join([res.rule_id, res.title, res.summary]).lower()
            status_hay = " ".join([res.status, res.declared_severity]).lower()
            if not _contains(want["rule"], rule_hay):
                continue

            matching = [f for f in res.findings if _finding_matches(f, want)]
            rule_status_ok = _contains(want["status"], status_hay)

            # A rule survives on its own text, or because a finding under it matches.
            if row_filters_active or want["status"]:
                if not matching and not (rule_status_ok and not row_filters_active):
                    continue
            elif not rule_status_ok:
                continue

            node = self.tree.insert(
                "", "end", text="%s  %s" % (res.rule_id, res.title),
                values=(STATUS_GLYPH[res.status], "",
                        ("%d finding%s" % (len(res.findings),
                                           "" if len(res.findings) == 1 else "s"))
                        if res.findings or res.category not in FILE_LEVEL_CATEGORIES
                        else "file-level",
                        res.summary),
                tags=(res.status, "rulerow"))
            rule_ids = []
            for f in res.findings:
                rule_ids.extend(f.all_ids)
            self.row_data[node] = {"kind": "rule", "rule": res.rule_id, "ids": rule_ids,
                                   "file_level": res.category in FILE_LEVEL_CATEGORIES}
            shown += 1

            listed = matching if (row_filters_active or want["status"]) else res.findings
            room = max(0, MAX_VISIBLE_FINDINGS - visible_findings)
            limit = min(MAX_ROWS_PER_RULE, room)
            for f in listed[:limit]:
                child = self.tree.insert(
                    node, "end",
                    text="    #%s" % f.ifc_id if f.ifc_id is not None else "    -",
                    values=("\u25cf " + str(f.severity), f.entity, f.name,
                            f.message + (("  |  " + f.detail) if f.detail else "")),
                    tags=("finding",))
                self.row_data[child] = {"kind": "finding", "rule": res.rule_id,
                                        "ids": f.all_ids, "finding": f}
                visible_findings += 1
            if len(listed) > limit:
                self.tree.insert(node, "end", text="    ...",
                                 values=("", "", "",
                                         "%d more findings not listed; export for the full set."
                                         % (len(listed) - limit)))
            if listed and (res.status == FAIL or row_filters_active) and len(listed) <= 60:
                self.tree.item(node, open=True)

        total = len(self.report.results)
        suffix = (" | %d findings shown" % visible_findings
                  if visible_findings else "")
        self.shown_lbl.configure(text="%d of %d rules%s" % (shown, total, suffix))

    def populate_objects(self):
        self.obj_tree.delete(*self.obj_tree.get_children())
        if self.report is None:
            return
        want_entity = self.obj_filters["entity"].get().strip().lower()
        want_rules = self.obj_filters["rules"].get().strip().lower()
        for row in self.report.object_checklist():
            if self.only_failing.get() and row["failing"] == 0:
                continue
            if not _contains(want_entity, row["entity"].lower()):
                continue
            if not _contains(want_rules, (row["rules"] or "").lower()):
                continue
            tag = PASS if row["failing"] == 0 else FAIL
            self.obj_tree.insert("", "end", text=row["entity"],
                                 values=(row["total"], row["conforming"],
                                         row["failing"], row["rules"]), tags=(tag,))
        for status, colour in STATUS_COLOUR.items():
            self.obj_tree.tag_configure(status, foreground=colour)

    def populate_breakdown(self):
        self.bd_tree.delete(*self.bd_tree.get_children())
        self.bd_nodes = {}
        if self.report is None:
            return
        want = dict((k, v.get().strip().lower()) for k, v in self.bd_filters.items())
        only_failing = self.bd_only_failing.get()
        tree = self.report.breakdown()

        for disc in sorted(tree):
            if not _contains(want["discipline"], disc.lower()):
                continue
            disc_node = self.bd_tree.insert("", "end", text=disc)
            disc_ids, disc_fail = [], 0

            for storey in sorted(tree[disc]):
                if not _contains(want["storey"], storey.lower()):
                    continue
                storey_node = self.bd_tree.insert(disc_node, "end", text=storey)
                storey_ids, storey_fail = [], 0

                for cls in sorted(tree[disc][storey]):
                    if not _contains(want["entity"], cls.lower()):
                        continue
                    node = tree[disc][storey][cls]
                    failing = len(node["failing"])
                    if only_failing and not failing:
                        continue
                    total = len(node["ids"])
                    item = self.bd_tree.insert(
                        storey_node, "end", text=cls,
                        values=(total, failing, total - failing,
                                ", ".join(sorted(node["rules"]))),
                        tags=(FAIL if failing else PASS,))
                    self.bd_nodes[item] = list(node["ids"])
                    storey_ids.extend(node["ids"])
                    storey_fail += failing

                if not self.bd_tree.get_children(storey_node):
                    self.bd_tree.delete(storey_node)
                    continue
                self.bd_tree.item(storey_node, values=(
                    len(storey_ids), storey_fail, len(storey_ids) - storey_fail, ""))
                self.bd_nodes[storey_node] = storey_ids
                disc_ids.extend(storey_ids)
                disc_fail += storey_fail

            if not self.bd_tree.get_children(disc_node):
                self.bd_tree.delete(disc_node)
                continue
            self.bd_tree.item(disc_node, open=True, values=(
                len(disc_ids), disc_fail, len(disc_ids) - disc_fail, ""))
            self.bd_nodes[disc_node] = disc_ids

    # -- actions ------------------------------------------------------------

    def _selected_ids(self):
        ids = []
        for item in self.tree.selection():
            data = self.row_data.get(item)
            if data:
                ids.extend(data["ids"])
        seen = set()
        out = []
        for i in ids:
            if i not in seen:
                seen.add(i)
                out.append(i)
        return out

    def on_row_select(self, _event=None):
        items = self.tree.selection()
        if len(items) != 1:
            return
        data = self.row_data.get(items[0])
        if not data:
            return
        if data["kind"] == "finding":
            self.set_status(self.describe_finding(data["finding"]))
            if data["finding"].ifc_id is not None:
                self.show_object(data["finding"].ifc_id)
        elif data.get("file_level"):
            self.set_status("%s is a file-level check: it concerns the IFC file as a "
                            "whole (header, georeferencing, units), not an object."
                            % data["rule"])
        if not self.autoselect.get() or data["kind"] != "finding":
            return
        self.select_current(quiet=True)

    def describe_finding(self, finding):
        """Where an object is, for use when MicroStation cannot select it."""
        if finding.ifc_id is None:
            return "%s: file-level finding, no object." % finding.rule_id
        parts = ["#%s %s" % (finding.ifc_id, finding.entity)]
        if finding.name:
            parts.append("'%s'" % finding.name)
        if finding.guid:
            parts.append("GlobalId %s" % finding.guid)
        ctx = getattr(self.report, "ctx", None)
        if ctx is not None:
            parts.append("storey %s" % ctx.storey_label(finding.ifc_id))
        extra = len(finding.all_ids) - 1
        if extra > 0:
            parts.append("+%d more affected element(s)" % extra)
        return "  \u00b7  ".join(parts)

    def copy_globalid(self):
        guids = []
        for item in self.tree.selection():
            data = self.row_data.get(item) or {}
            finding = data.get("finding")
            if finding is not None and finding.guid and finding.guid not in guids:
                guids.append(finding.guid)
        if not guids:
            self.set_status("Select one or more object rows first.")
            return
        self.clipboard_clear()
        self.clipboard_append("\n".join(guids))
        self.set_status("Copied %d GlobalId(s) to the clipboard." % len(guids))

    def on_row_activate(self, _event=None):
        self.select_current(quiet=True)

    def on_class_activate(self, _event=None):
        sel = self.obj_tree.selection()
        if not sel or self.report is None:
            return
        cls = self.obj_tree.item(sel[0], "text")
        ids = []
        for res in self.report.results:
            for f in res.findings:
                if f.severity != "ERROR":
                    continue
                for oid in f.all_ids:
                    if self.ifc.type_of(oid) == cls:
                        ids.append(oid)
        self._do_select(sorted(set(ids)))

    def on_breakdown_activate(self, _event=None):
        sel = self.bd_tree.selection()
        if not sel:
            return
        ids = self.bd_nodes.get(sel[0]) or []
        if not ids:
            self.set_status("That row has no locatable elements.")
            return
        self._do_select(sorted(set(ids)))

    def select_current(self, quiet=False):
        ids = self._selected_ids()
        if not ids:
            if not quiet:
                self.set_status("Nothing selected in the list.")
            return
        self._do_select(ids)

    def select_rule(self):
        items = self.tree.selection()
        if not items:
            self.set_status("Select a rule row first.")
            return
        data = self.row_data.get(items[0])
        if not data:
            return
        ids = []
        for res in self.report.results:
            if res.rule_id != data["rule"]:
                continue
            for f in res.findings:
                ids.extend(f.all_ids)
            break
        self._do_select(sorted(set(ids)))

    def _do_select(self, ids):
        if not self._locator_ready():
            return
        if not ids:
            self.set_status("No locatable objects in that group.")
            return
        _count, note = self.locator.select(ids)
        self.set_status(note)

    def isolate_current(self):
        """Show only the geometry behind the selected findings."""
        if not self._locator_ready():
            return
        ids = self._selected_ids()
        if not ids:
            self.set_status("Nothing selected in the list.")
            return
        _count, note = self.locator.isolate(ids)
        self.set_status(note)

    def show_all(self):
        """Clear the display set so the whole model is visible again."""
        if not self._locator_ready():
            return
        _ok, note = self.locator.show_all()
        self.set_status(note)

    def clear_selection(self):
        if not self._locator_ready():
            return
        try:
            from MSPyDgnView import SelectionSetManager
            manager = SelectionSetManager.GetManager()
            if manager is None:
                self.set_status("MicroStation selection manager is unavailable.")
                return
            manager.EmptyAll()
            self.set_status("Selection cleared.")
        except Exception as exc:
            self.set_status("Could not clear selection: %s" % exc)

    def _locator_ready(self):
        if self.locator is None:
            self.set_status("Run a check first.")
            return False
        if not self.locator.available:
            self.set_status("Locate unavailable: %s" % self.locator.reason)
            return False
        return True

    # -- export -------------------------------------------------------------

    def export(self, fmt):
        if self.report is None:
            self.set_status("Run a check first.")
            return
        default = report_mod.default_export_path(self.source.path, fmt)
        path = filedialog.asksaveasfilename(
            title="Export report", defaultextension="." + fmt, parent=self,
            initialfile=os.path.basename(default),
            initialdir=os.path.dirname(default))
        if not path:
            return
        try:
            if fmt == "json":
                self.report.write_json(path)
            elif fmt == "csv":
                self.report.write_csv(path)
            elif fmt == "html":
                self.report.write_html(path, pictures=self.report_pictures())
                self.last_html = path
            else:
                self.report.write_text(path)
            self.set_status("Written: %s" % path)
            if fmt == "html" and messagebox.askyesno(
                    "Export HTML", "Report written.\n\nOpen it in your browser now?",
                    parent=self):
                webbrowser.open("file:///" + path.replace("\\", "/"))
        except Exception as exc:
            messagebox.showerror("IFC+SG Checker", "Export failed:\n%s" % exc, parent=self)

    def export_bcf(self):
        """Write BCF 2.1 topics for every failing or warning rule."""
        if self.report is None:
            self.set_status("Run a check first.")
            return
        default = report_mod.default_export_path(self.source.path, "bcfzip")
        path = filedialog.asksaveasfilename(
            title="Export BCF", defaultextension=".bcfzip", parent=self,
            initialfile=os.path.basename(default),
            initialdir=os.path.dirname(default),
            filetypes=[("BCF archive", "*.bcfzip"), ("All files", "*.*")])
        if not path:
            return
        try:
            _written, topics = bcf_mod.write_bcf(self.report, path)
        except Exception as exc:
            messagebox.showerror("IFC+SG Checker", "BCF export failed:\n%s" % exc,
                                 parent=self)
            return
        if not topics:
            self.set_status("Nothing to report: no failing or warning rules.")
            return
        self.set_status("Wrote %d BCF topic(s): %s" % (topics, path))

    def open_html(self):
        """Write the HTML report beside the source file and open it."""
        if self.report is None:
            self.set_status("Run a check first.")
            return
        try:
            if not self.last_html or not os.path.isfile(self.last_html):
                self.last_html = self.report.write_html(
                    report_mod.default_export_path(self.source.path, "html"),
                    pictures=self.report_pictures())
            webbrowser.open("file:///" + self.last_html.replace("\\", "/"))
            self.set_status("Opened %s" % self.last_html)
        except Exception as exc:
            messagebox.showerror("IFC+SG Checker", "Could not open report:\n%s" % exc,
                                 parent=self)

    def set_status(self, text):
        self.status.configure(text=str(text))
        try:
            self.update_idletasks()
        except Exception:
            pass


def launch(library_root=None, dock=True, cop=None):
    if not HAS_TK:
        raise RuntimeError("tkinter is not available in this Python environment.")
    existing = getattr(builtins, SINGLETON_NAME, None)
    if existing is not None:
        try:
            if existing.winfo_exists():
                existing.present()
                return existing
        except Exception:
            pass
        try:
            delattr(builtins, SINGLETON_NAME)
        except AttributeError:
            pass
    window = CheckerWindow(library_root, dock=dock, cop=cop)
    setattr(builtins, SINGLETON_NAME, window)
    MstnTk.run()
    return window
