"""Regression harness for the IFC+SG checker.

Run from MicroStation:
    python load "$(IFCSG_CHECKER_DIR)tools\\microstation\\selftest.py"

Checks the parser, the rule engine, the report and the MicroStation locator against
whichever IFC source the session already has, then writes the outcome to
%TEMP%\\ifcsg\\selftest.log. Pass --ifc <path> to test a specific file instead.
"""

import importlib
import os
import sys
import traceback


def _config_var(name):
    try:
        from MSPyBentley import WString
        from MSPyDgnPlatform import ConfigurationManager, ConfigurationVariableLevel
        value = WString()
        ConfigurationManager.GetVariable(value, name, ConfigurationVariableLevel.eUser)
        return str(value)
    except Exception:
        return None


# `python load` resolves a script by basename, so __file__ may name a different
# copy. The installed location wins when IFCSG_CHECKER_DIR is defined.
HERE = os.path.dirname(os.path.abspath(__file__))
for _root in (_config_var("IFCSG_CHECKER_DIR"), os.environ.get("IFCSG_CHECKER_DIR")):
    if not _root:
        continue
    _cand = os.path.join(_root.strip().rstrip("\\/"), "tools", "microstation")
    if os.path.isfile(os.path.join(_cand, "ifcsg_checker", "__init__.py")):
        HERE = _cand
        break

while HERE in sys.path:
    sys.path.remove(HERE)
sys.path.insert(0, HERE)
for _n in [m for m in list(sys.modules) if m == "ifcsg_checker" or m.startswith("ifcsg_checker.")]:
    del sys.modules[_n]
importlib.invalidate_caches()

OUT_DIR = os.path.join(os.environ.get("TEMP", os.path.expanduser("~")), "ifcsg")
LOG = os.path.join(OUT_DIR, "selftest.log")
MAX_LOCATOR_SAMPLES_PER_CLASS = 5

lines = []


def say(msg):
    lines.append(str(msg))


def explicit_ifc():
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--ifc" and i + 1 < len(argv):
            return argv[i + 1]
    return None


try:
    say("python %s" % sys.version.replace("\n", " "))
    say("script:        %s" % os.path.abspath(__file__))
    say("package dir:   %s" % HERE)

    import ifcsg_checker
    from ifcsg_checker.library import Library
    from ifcsg_checker.spf import IfcFile
    from ifcsg_checker.rules import Engine
    from ifcsg_checker import report as report_mod
    from ifcsg_checker import sources as sources_mod
    from ifcsg_checker.locate import Locator
    say("imports OK, version %s" % ifcsg_checker.__version__)
    say("package:       %s" % os.path.dirname(ifcsg_checker.__file__))

    try:
        import tkinter                                    # noqa: F401
        say("tkinter OK")
    except Exception as exc:
        say("tkinter MISSING: %s" % exc)

    lib = Library.discover()
    say("library root:  %s" % lib.root)
    say("library errors: %s" % (lib.load_errors or "none"))
    say("editions:      %s" % lib.editions)

    found = sources_mod.discover()
    say("in_microstation=%s import_error=%s"
        % (sources_mod.IN_MICROSTATION, sources_mod.IMPORT_ERROR or "none"))
    say("sources: %d" % len(found))
    for s in found:
        say("   %-46s kind=%-14s ifc=%-5s locatable=%s"
            % (s.label, s.kind, s.is_ifc, s.is_locatable))

    override = explicit_ifc()
    target = None
    if override:
        target = sources_mod.browsed_source(override)
    else:
        for s in found:
            if s.is_ifc:
                target = s
                break
    if target is None:
        say("No IFC source in the session and no --ifc given; nothing to check.")
        raise SystemExit(0)

    ifc = IfcFile.read(target.path)
    say("")
    say("parsed %d entities  schema=%s  mvd=%s"
        % (ifc.count(), ifc.schema, "; ".join(ifc.view_definitions) or "none"))

    ctx, results = Engine(lib).run(ifc)
    say("elements indexed: %d across %d classes"
        % (len(ctx.element_ids), len(ctx.element_classes)))

    rep = report_mod.Report(target, ifc, ctx, results, lib)
    say("VERDICT: %s" % rep.verdict)
    say("")
    say("--- rule checklist ---")
    for r in rep.results:
        say("%-14s %-12s %-5d %s" % (r.status, r.rule_id, r.count, r.summary[:140]))
    say("")
    say("--- object checklist ---")
    for row in rep.object_checklist():
        say("%-26s total=%-5d conforming=%-5d failing=%-5d %s"
            % (row["entity"], row["total"], row["conforming"], row["failing"], row["rules"]))

    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)
    rep.write_text(os.path.join(OUT_DIR, "selftest_report.txt"))
    rep.write_csv(os.path.join(OUT_DIR, "selftest_report.csv"))
    rep.write_json(os.path.join(OUT_DIR, "selftest_report.json"))
    say("")
    say("exports written to %s" % OUT_DIR)

    loc = Locator(target, ifc, ctx.geo)
    say("locator available=%s reason=%s metre_scale=%s"
        % (loc.available, loc.reason or "-", loc.metre_scale))
    if loc.available:
        say("")
        say("--- locator resolution sample by class ---")
        for cls in sorted(ctx.element_classes):
            ids = [e.id for e in ifc.of_type(cls)]
            sample = ids[:MAX_LOCATOR_SAMPLES_PER_CLASS]
            resolved = sum(1 for i in sample if loc.matches(i))
            flag = "" if resolved == len(sample) else "   <-- incomplete sample"
            say("%-26s ifc=%-5d sampled=%-3d resolved=%-3d%s"
                % (cls, len(ids), len(sample), resolved, flag))
        loc.close()

    say("")
    say("SELFTEST OK")
except SystemExit:
    pass
except Exception:
    say("SELFTEST FAILED")
    say(traceback.format_exc())

try:
    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)
    with open(LOG, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
except Exception:
    pass
