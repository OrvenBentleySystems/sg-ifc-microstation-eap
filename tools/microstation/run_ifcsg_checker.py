"""Launcher for the IFC+SG checker.

From MicroStation:
    key-in:  python load $(IFCSG_CHECKER_LAUNCHER)

Headless (for regression runs, no CAD licence needed):
    python run_ifcsg_checker.py --ifc "model.ifc" --out report.txt
"""

import builtins
import importlib
import os
import sys


def _tool_dir():
    """Folder holding the ifcsg_checker package.

    MicroStation's `python load` resolves a script by basename, so __file__ can
    point at a different copy of this launcher than the one that was asked for.
    The IFCSG_CHECKER_DIR configuration variable, written by the installer, is
    authoritative; __file__ is only the fallback for an uninstalled checkout.
    """
    for root in (_config_var("IFCSG_CHECKER_DIR"), os.environ.get("IFCSG_CHECKER_DIR")):
        if not root:
            continue
        candidate = os.path.join(root.strip().rstrip("\\/"), "tools", "microstation")
        if os.path.isfile(os.path.join(candidate, "ifcsg_checker", "__init__.py")):
            return candidate
    return os.path.dirname(os.path.abspath(__file__))


def _config_var(name):
    try:
        from MSPyBentley import WString
        from MSPyDgnPlatform import ConfigurationManager, ConfigurationVariableLevel
        value = WString()
        ConfigurationManager.GetVariable(value, name, ConfigurationVariableLevel.eUser)
        return str(value)
    except Exception:
        return None


HERE = _tool_dir()
while HERE in sys.path:
    sys.path.remove(HERE)
sys.path.insert(0, HERE)

_existing_window = getattr(builtins, "_ifcsg_checker_window", None)
try:
    _existing_window_alive = bool(
        _existing_window is not None and _existing_window.winfo_exists())
except Exception:
    _existing_window_alive = False
    try:
        delattr(builtins, "_ifcsg_checker_window")
    except AttributeError:
        pass

if not _existing_window_alive:
    # MicroStation keeps modules in sys.modules for the life of the process, so an
    # edited file would otherwise keep running the previously imported code. Never
    # purge modules underneath a live checker window or its background worker.
    for _name in [m for m in list(sys.modules) if m == "ifcsg_checker"
                  or m.startswith("ifcsg_checker.")]:
        del sys.modules[_name]

# A just-redeployed folder can otherwise be served from a stale directory listing.
importlib.invalidate_caches()

import ifcsg_checker                                     # noqa: E402
from ifcsg_checker.library import Library                # noqa: E402
from ifcsg_checker.rules import Engine                   # noqa: E402
from ifcsg_checker.spf import IfcFile                    # noqa: E402
from ifcsg_checker import report as report_mod           # noqa: E402
from ifcsg_checker import sources as sources_mod         # noqa: E402


def run_headless(ifc_path, out_path=None, library_root=None):
    lib = Library.discover(library_root)
    ifc = IfcFile.read(ifc_path)
    ctx, results = Engine(lib).run(ifc)
    source = sources_mod.browsed_source(ifc_path)
    rep = report_mod.Report(source, ifc, ctx, results, lib)
    text = rep.as_text()
    if out_path:
        stem = os.path.splitext(out_path)[0]
        rep.write_text(out_path)
        rep.write_csv(stem + ".csv")
        rep.write_json(stem + ".json")
        rep.write_html(stem + ".html")
    return rep, text


def main(argv):
    library_root = None
    ifc_path = None
    out_path = None
    dock = True
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--ifc" and i + 1 < len(argv):
            ifc_path = argv[i + 1]
            i += 1
        elif arg == "--out" and i + 1 < len(argv):
            out_path = argv[i + 1]
            i += 1
        elif arg == "--library" and i + 1 < len(argv):
            library_root = argv[i + 1]
            i += 1
        elif arg in ("--nodock", "--no-dock"):
            dock = False
        i += 1

    if ifc_path:
        _rep, text = run_headless(ifc_path, out_path, library_root)
        sys.stdout.write(text + "\n")
        return 0

    if _existing_window_alive:
        _existing_window.present()
        return 0

    from ifcsg_checker.ui import launch
    launch(library_root, dock=dock)
    return 0


def _report_to_microstation(message):
    try:
        from MSPyMstnPlatform import MessageCenter
        MessageCenter.ShowErrorMessage("IFC+SG Checker", message, False)
    except Exception:
        sys.stderr.write(message + "\n")


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as exc:
        import traceback
        _report_to_microstation("%s: %s" % (type(exc).__name__, exc))
        traceback.print_exc()
        raise
