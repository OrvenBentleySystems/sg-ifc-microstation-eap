"""Headless layout assertions for the checker window.

Runs outside MicroStation with stock CPython, so the tkinter geometry that
caused clipped rows and a misread second header can be checked without a CAD
licence.  Run:  python uicheck.py
"""

import os
import sys
import tempfile
import tkinter as tk
import time
from tkinter import font as tkfont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ifcsg_checker import ui as ui_mod  # noqa: E402
from ifcsg_checker import spf as spf_mod  # noqa: E402
from ifcsg_checker import sources as sources_mod  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests", "fixture.ifc")

FAILURES = []


def check(label, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + label + ("   " + detail if detail else ""))
    if not ok:
        FAILURES.append(label)


def main():
    ui_mod.SETTINGS_PATH = os.path.join(
        tempfile.mkdtemp(prefix="ifcsg-ui-"), "window.json")
    win = ui_mod.CheckerWindow(dock=False)
    win.update_idletasks()

    print("Row height vs font")
    line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
    from tkinter import ttk
    rh = int(ttk.Style(win).lookup("Treeview", "rowheight") or 0)
    check("rowheight clears the font line height", rh >= line + 4,
          "rowheight=%d linespace=%d" % (rh, line))

    print("Filter bar is one row, not a fake header")
    bar = win.col_filters
    check("rules tab keeps all five filters", sorted(bar) ==
          ["entity", "message", "name", "rule", "status"], ",".join(sorted(bar)))
    check("objects tab keeps both filters", sorted(win.obj_filters) == ["entity", "rules"])

    entries = [w for w in _descend(win.rules_tab) if w.winfo_class() == "TEntry"]
    tops = set(w.winfo_y() for w in entries if w.winfo_ismapped())
    check("filter boxes share one row", len(tops) <= 1, "distinct y: %d" % len(tops))

    print("Breakdown tab")
    check("breakdown tree exists", hasattr(win, "bd_tree"))
    check("breakdown has three filters",
          sorted(getattr(win, "bd_filters", {})) == ["discipline", "entity", "storey"],
          ",".join(sorted(getattr(win, "bd_filters", {}))))
    bd_entries = [w for w in _descend(win.breakdown_tab) if w.winfo_class() == "TEntry"]
    bd_tops = set(w.winfo_y() for w in bd_entries if w.winfo_ismapped())
    check("breakdown filters share one row", len(bd_tops) <= 1,
          "distinct y: %d" % len(bd_tops))
    check("breakdown survives having no report", _safe(win.populate_breakdown))

    labels = [w.cget("text") for w in _descend(win) if w.winfo_class() == "TButton"]
    check("BCF export button present", "Export BCF" in labels)
    check("unsupported zoom button removed", "Zoom to" not in labels)
    check("large-model autoselect defaults off", not win.autoselect.get())

    print("Clear filters")
    for var in win.col_filters.values():
        var.set("x")
    for var in win.show.values():
        var.set(False)
    win.clear_filters()
    check("text filters clear",
          all(not var.get() for var in win.col_filters.values()))
    check("default result statuses restore",
          all(win.show[status].get() for status in
              (ui_mod.FAIL, ui_mod.WARN, ui_mod.UNKNOWN))
          and not win.show[ui_mod.PASS].get()
          and not win.show[ui_mod.NOT_APPLICABLE].get())
    check("clear filters leaves no queued rebuild",
          "rules" not in win._filter_jobs)

    print("Background work stays on the Tk lifecycle")
    completed = []

    def work(cancel, progress):
        for step in range(4):
            if cancel():
                raise spf_mod.OperationCancelled("cancelled")
            progress("step %d" % step, step, 4)
            time.sleep(0.02)
        return 42

    win._start_worker(work, completed.append)
    responsive = _pump_until(win, lambda: not win.busy)
    check("worker completes while Tk keeps processing", responsive)
    check("worker result returns on the Tk thread", completed == [42], str(completed))

    win._start_worker(work, completed.append)
    win.cancel_work()
    cancelled = _pump_until(win, lambda: not win.busy)
    check("worker cancellation completes cleanly", cancelled)
    check("cancelled worker does not publish a result", completed == [42], str(completed))

    print("COP selector")
    labels = list(win.cop_box["values"])
    check("selector lists every installed edition", len(labels) >= 2, " / ".join(labels))
    check("newest edition preselected",
          win.library.editions["cop_edition"] == "4" and win.cop_box.current() == 0)
    check("no warning banner on the newest edition", not win.warn_lbl.winfo_ismapped())
    win.update()
    summary_y = win.summary_frame.winfo_y()
    win.sources = [sources_mod.browsed_source(FIXTURE)]
    win.source_box["values"] = [s.label for s in win.sources]
    win.source_box.current(0)
    win.run_check()
    check("fixture check completes", _pump_until(win, lambda: not win.busy, 15.0)
          and win.report is not None)
    first = win.report.header()["cop_edition"] if win.report else None
    print("Objects to fix and preview")
    fix_rows = win.fix_tree.get_children()
    check("objects-to-fix tab lists every object to fix",
          len(fix_rows) == len(win.report.failing_objects()), str(len(fix_rows)))
    check("objects-to-fix tab opens after a check",
          win.notebook.select() == str(win.fix_tab))
    _pump_until(win, lambda: win.preview_id is not None and win._preview_job is None, 3.0)
    top = win.report.failing_objects()[0]
    check("first object is shown without a click", win.preview_id == top["id"])
    shown = win.preview_text.get("1.0", "end")
    check("preview lists every reason for the object",
          all(i["rule"] in shown for i in top["issues"]), shown[:120])
    check("preview names the object", ("#%s" % top["id"]) in win.preview_title.cget("text"))
    check("preview explains a missing picture",
          bool(win._preview_wire) or bool(win._preview_note), win._preview_note)
    finding_row = next(k for k, d in win.row_data.items() if d["kind"] == "finding"
                       and d["finding"].ifc_id not in (None, top["id"]))
    win.tree.selection_set(finding_row)
    win.on_row_select()
    check("selecting a finding previews its object",
          win.preview_id == win.row_data[finding_row]["finding"].ifc_id)
    win.fix_filter.set("zz-no-match-zz")
    _pump_until(win, lambda: not win.fix_tree.get_children(), 2.0)
    check("find box filters objects", not win.fix_tree.get_children())
    win.fix_filter.set("")
    _pump_until(win, lambda: bool(win.fix_tree.get_children()), 2.0)
    chip = win.chips[ui_mod.FAIL].cget("text")
    check("status chips show counts after a check", chip.split()[-1].isdigit(), chip)
    older = next(i for i, e in enumerate(win.editions) if e.cop_edition == "3.1")
    win.cop_box.current(older)
    win.on_cop_change()
    check("changing COP re-checks the parsed model",
          _pump_until(win, lambda: not win.busy, 15.0)
          and win.report is not None
          and win.report.header()["cop_edition"] == "3.1" and first == "4")
    win.update()
    check("superseded edition shows the warning banner",
          win.warn_lbl.winfo_ismapped() and "superseded" in win.warn_lbl.cget("text"))
    check("COP choice is remembered",
          ui_mod._load_settings().get("cop") == "3.1")
    win.cop_box.current(0)
    win.on_cop_change()
    _pump_until(win, lambda: not win.busy, 15.0)
    win.update()
    check("returning to the newest edition clears the banner",
          not win.warn_lbl.winfo_ismapped())
    check("no blank gap is left where the banner was",
          win.summary_frame.winfo_y() == summary_y,
          "verdict y %d, originally %d" % (win.summary_frame.winfo_y(), summary_y))

    print("Every row says where its object is")
    finding_rows = [d for d in win.row_data.values() if d["kind"] == "finding"]
    sample = finding_rows[0]["finding"] if finding_rows else None
    text = win.describe_finding(sample) if sample else ""
    check("finding row reports GlobalId and storey",
          "GlobalId" in text and "storey" in text, text)
    file_level = [k for k, d in win.row_data.items() if d["kind"] == "rule" and not d["ids"]]
    if file_level:
        win.tree.selection_set(file_level[0])
        win.on_row_select()
        check("file-level rule says it has no object",
              "file-level" in win.status.cget("text"), win.status.cget("text"))
    labels = [w.cget("text") for w in _descend(win) if w.winfo_class() == "TButton"]
    check("Copy GlobalId action present", "Copy GlobalId" in labels)

    real_engine, real_error = ui_mod.Engine, ui_mod.messagebox.showerror
    for label, exc in (("cancelled", spf_mod.OperationCancelled("stop")),
                       ("failed", RuntimeError("boom"))):
        class _Stub(object):
            def __init__(self, _lib):
                pass

            def run(self, *_a, **_k):
                raise exc
        ui_mod.Engine = _Stub
        ui_mod.messagebox.showerror = lambda *a, **k: None
        try:
            win.cop_box.current(older)
            win.on_cop_change()
            _pump_until(win, lambda: not win.busy, 15.0)
        finally:
            ui_mod.Engine, ui_mod.messagebox.showerror = real_engine, real_error
        check("%s re-check keeps the previous COP and its results" % label,
              win.library.editions["cop_edition"] == "4"
              and win.report.header()["cop_edition"] == "4"
              and win.cop_box.current() == 0
              and ui_mod._load_settings().get("cop") == "4")

    print("Columns fit the window")
    fixed = sum(win.tree.column(c, "width") for c in ("#0", "status", "entity", "name"))
    check("fixed columns fit a narrow window", fixed <= 700, "fixed=%dpx" % fixed)
    check("detail column stretches", bool(win.tree.column("message", "stretch")))

    print("Window can be made small")
    mn = (win.minsize()[0], win.minsize()[1])
    check("minsize fits a laptop screen", mn[0] <= 800 and mn[1] <= 560, str(mn))

    win._close_now()
    print("\nFAILURES: %d" % len(FAILURES))
    for f in FAILURES:
        print("  - " + f)
    return 1 if FAILURES else 0


def _descend(widget):
    for child in widget.winfo_children():
        yield child
        for grand in _descend(child):
            yield grand


def _safe(fn):
    try:
        fn()
        return True
    except Exception as exc:
        print("       raised: %s: %s" % (type(exc).__name__, exc))
        return False


def _pump_until(win, predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        win.update()
        if predicate():
            return True
        time.sleep(0.01)
    return False


if __name__ == "__main__":
    sys.exit(main())
