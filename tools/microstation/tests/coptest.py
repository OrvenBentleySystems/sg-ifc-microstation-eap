"""COP edition selection, COP 4 content and edition-to-edition rebuilds."""

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(TOOLS))
sys.path.insert(0, TOOLS)
sys.path.insert(0, HERE)

from ifcsg_checker.library import Library, cop_key  # noqa: E402
from ifcsg_checker.rules import Engine, PASS, WARN  # noqa: E402
from ifcsg_checker.spf import IfcFile  # noqa: E402
from ifcsg_checker import report as report_mod  # noqa: E402
from ifcsg_checker import sources as sources_mod  # noqa: E402
from cataloguetest import _write_xlsx  # noqa: E402

FAILURES = []
FIXTURE = os.path.join(HERE, "fixture.ifc")


def check(label, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + label + ("   " + detail if detail else ""))
    if not ok:
        FAILURES.append(label)


def _props(library, pset):
    entry = library.sgpsets.get(pset) or {}
    return dict((p["name"], p) for p in entry.get("properties", []))


def editions():
    print("Edition discovery")
    found = Library.available(ROOT)
    cops = [item.cop_edition for item in found]
    check("both editions installed, newest first", cops[:2] == ["4", "3.1"], str(cops))
    check("cop keys order numerically", cop_key("10") > cop_key("4") > cop_key("3.1"))
    default = Library.discover(ROOT)
    check("newest edition is the default", default.editions["cop_edition"] == "4")
    check("newest edition is not superseded", not default.editions["superseded"])
    old = Library.discover(ROOT, cop="3.1")
    check("older edition loads on request", old.editions["cop_edition"] == "3.1")
    check("older edition is flagged superseded",
          old.editions["superseded"] and old.editions["latest_cop"] == "4")
    try:
        Library.discover(ROOT, cop="99")
        check("unknown edition is rejected", False)
    except ValueError as exc:
        check("unknown edition is rejected", "not installed" in str(exc))

    with tempfile.TemporaryDirectory(prefix="ifcsg-legacy-") as folder:
        os.makedirs(os.path.join(folder, "data"))
        shutil.copyfile(os.path.join(ROOT, "data", "catalogues", "cop-3.1.json"),
                        os.path.join(folder, "data", "catalogue.json"))
        legacy = Library.discover(folder)
        check("legacy single-file layout still loads",
              legacy.editions["cop_edition"] == "3.1" and not legacy.is_superseded)


def cop4_content():
    print("COP 4 catalogue content")
    lib = Library.discover(ROOT, cop="4")
    meta = lib.metadata
    check("mapping edition is the September 2026 workbook",
          meta["mapping_edition"] == "2026-09-25")
    check("COP 4 publication recorded", meta.get("cop_published") == "2026-09")
    check("workbook source recorded",
          any(s["name"] == "industry-mapping-09-2026.xlsx" for s in meta["sources"]))
    check("withdrawn SGPset_Door.SelfClosing removed",
          "SelfClosing" not in _props(lib, "SGPset_Door"))
    check("SelfClosing now in Pset_DoorCommon",
          "SelfClosing" in _props(lib, "Pset_DoorCommon"))
    check("IsExternal moved to Pset_WallCommon",
          "IsExternal" in _props(lib, "Pset_WallCommon")
          and "IsExternal" not in _props(lib, "SGPset_Wall"))
    check("DoubleBayFacade renamed", "DoubleBayFacade" in _props(lib, "SGPset_Wall")
          and "DoubleBay\u0046a\u00e7ade" not in _props(lib, "SGPset_Wall"))
    activity = _props(lib, "SGPset_Space").get("IndustrialActivityType", {})
    values = activity.get("enum_values", [])
    check("IndustrialActivityType enum resolved from its sheet",
          len(values) > 100 and values[0].startswith("SS593-AnnexA")
          and not any(v.lower().startswith("refer to") for v in values),
          "%d values" % len(values))
    check("COP 4 adds AmendmentStatus", "AmendmentStatus" in _props(lib, "SGPset_Wall"))
    check("COP 4 retypes SGPset_Pump.PumpHead",
          _props(lib, "SGPset_Pump")["PumpHead"]["type"] == "Length")
    check("identified components replaced by the workbook",
          len(lib.identified_components) == 131, str(len(lib.identified_components)))
    old = Library.discover(ROOT, cop="3.1")
    check("COP 3.1 is unchanged by the COP 4 build",
          "SelfClosing" in _props(old, "SGPset_Door")
          and old.editions["mapping_edition"] == "2025-12-04")


def checks_follow_edition():
    print("Checks follow the selected edition")
    ifc = IfcFile.read(FIXTURE)
    source = sources_mod.browsed_source(FIXTURE)
    out = {}
    for cop in ("4", "3.1"):
        lib = Library.discover(ROOT, cop=cop)
        ctx, results = Engine(lib).run(ifc)
        rep = report_mod.Report(source, ifc, ctx, results, lib)
        out[cop] = (rep, dict((r.rule_id, r) for r in results))
    check("same parsed IFC re-runs against a second edition",
          bool(out["4"][1]) and bool(out["3.1"][1]))
    check("DOC.001 passes on the newest edition",
          out["4"][1]["DOC.001"].status == PASS, out["4"][1]["DOC.001"].status)
    check("DOC.001 warns on a superseded edition",
          out["3.1"][1]["DOC.001"].status == WARN
          and "superseded" in out["3.1"][1]["DOC.001"].summary)
    header = out["3.1"][0].header()
    check("report header stamps the edition",
          header["cop_edition"] == "3.1" and header["cop_superseded"] is True)
    check("text report renders", "cop_edition:           3.1" in out["3.1"][0].as_text())
    pset4 = " ".join(f.message for f in out["4"][1]["PSET.001"].findings)
    pset3 = " ".join(f.message for f in out["3.1"][1]["PSET.001"].findings)
    check("COP 4 requires Pset_WallCommon, COP 3.1 does not",
          "Pset_WallCommon" in pset4 and "Pset_WallCommon" not in pset3)


def edition_rebuild():
    print("New edition rebuild removes withdrawn properties")
    with tempfile.TemporaryDirectory(prefix="ifcsg-rebuild-") as folder:
        root = os.path.join(folder, "root")
        shutil.copytree(os.path.join(ROOT, "data"), os.path.join(root, "data"))
        previous = os.path.join(folder, "previous.xlsx")
        current = os.path.join(folder, "current.xlsx")
        _write_xlsx(previous)
        _write_xlsx(current)
        # The previous workbook also published FireRating on SGPset_Test; the
        # current one renames the set, so FireRating must leave SGPset_Test.
        _rename_set(current, "SGPset_Test", "SGPset_Test2")
        run = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "build_catalogue.py"),
             previous, "--root", root, "--cop-edition", "4", "--mapping-edition",
             "2026-01-01"],
            capture_output=True, text=True)
        check("seed workbook imports into existing edition", run.returncode == 0,
              run.stderr[-300:])
        run = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "build_catalogue.py"),
             current, "--root", root, "--cop-edition", "5", "--from", "4",
             "--mapping-edition", "2027-01-01", "--previous-mapping", previous,
             "--cop-published", "2027-01"],
            capture_output=True, text=True)
        check("--from creates a new edition", run.returncode == 0, run.stderr[-300:])
        new = Library.discover(root)
        check("new edition becomes the default", new.editions["cop_edition"] == "5")
        check("withdrawn property removed from the new edition",
              "SGPset_Test" not in new.sgpsets
              and "FireRating" in _props(new, "SGPset_Test2"))
        check("removal is recorded",
              "SGPset_Test.FireRating" in new.metadata.get("removed_properties", []))
        old = Library.discover(root, cop="4")
        check("previous edition keeps its definition",
              "FireRating" in _props(old, "SGPset_Test"))
        check("previous edition now superseded", old.is_superseded)
        run = subprocess.run(
            [sys.executable, os.path.join(TOOLS, "run_ifcsg_checker.py"),
             "--library", root, "--list-cops"], capture_output=True, text=True)
        check("launcher lists every edition",
              [line.split()[0] for line in run.stdout.splitlines()] == ["5", "4", "3.1"],
              run.stdout.strip().replace("\n", " / "))


def _rename_set(path, old, new):
    import zipfile
    with zipfile.ZipFile(path) as zf:
        items = dict((name, zf.read(name)) for name in zf.namelist())
    key = "xl/worksheets/sheet1.xml"
    items[key] = items[key].replace(old.encode(), new.encode())
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in items.items():
            zf.writestr(name, data)


def main():
    editions()
    cop4_content()
    checks_follow_edition()
    edition_rebuild()
    print("\nFAILURES: %d" % len(FAILURES))
    for failure in FAILURES:
        print("  - " + failure)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
